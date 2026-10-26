"""V2-specific integration/failure tests, independent of the V1 test bridge."""
import asyncio
from datetime import datetime, timedelta, timezone
import importlib
import json
import logging
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import RetryAfter, TimedOut

from jzbot.app import build_application
from jzbot.config import Settings
from jzbot.db.database import SchemaMismatch
from jzbot.db.reconciliation import assert_preserved, snapshot
from jzbot.domain import parse_amount_to_tenths
from jzbot.infra.logging import RedactingFormatter
from jzbot.repositories.store import LedgerStore
from jzbot.runtime import Runtime
from jzbot.services.accounting import AccountingService
from jzbot.services.connections import ConnectionsService
from jzbot.services.outbox import OutboxService
from scripts.verify_migration import rehearse


def settings(path):
    return Settings(str(123456789)+":"+"X"*35, path,
                    path.parent/'.jzbot.instance.lock', path.parent/'test.log', False, 'fixture-release')


def context():
    return SimpleNamespace(bot=SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=777)),
        pin_chat_message=AsyncMock()), user_data={})


@pytest.mark.parametrize('text', ['', ' ', '@', '+0', '-0.0', '+1.00', '+.5', '1',
                                  '+1e3', '+nan', '+inf', '+1,000', '--1', '+-1', '+1.5x'])
def test_invalid_amounts_remain_ignored(text):
    assert parse_amount_to_tenths(text) is None


@pytest.mark.parametrize('amount', [0, True, False, 0.1, '10', None])
async def test_service_and_repository_reject_noninteger_or_zero(ledger_db, owner_id, customer_id, amount):
    before = snapshot(ledger_db.db_path)
    for service in (ledger_db, AccountingService(ledger_db)):
        with pytest.raises(ValueError):
            await service.apply_amount(owner_id, customer_id, 1, amount, 'business-1')
    assert_preserved(before, snapshot(ledger_db.db_path))


async def test_service_rejects_other_owners_connection(ledger_db, owner_id, customer_id):
    await ledger_db.upsert_business_connection('other', owner_id+1, True)
    before = snapshot(ledger_db.db_path)
    with pytest.raises(PermissionError):
        await AccountingService(ledger_db).apply_amount(owner_id, customer_id, 1, 100, 'other')
    assert_preserved(before, snapshot(ledger_db.db_path))


async def test_concurrent_commands_and_duplicate_across_two_connections(tmp_path):
    first = LedgerStore(tmp_path/'concurrent.db')
    second = LedgerStore(first.db_path)
    await first.open()
    await first.upsert_business_connection('b', 1, True)
    await second.open()
    try:
        await asyncio.gather(first.apply_amount(1, 2, 1, 150, 'b'),
                             second.apply_amount(1, 2, 2, -30, 'b'))
        results = await asyncio.gather(first.apply_amount(1, 2, 3, 5, 'b'),
                                       second.apply_amount(1, 2, 3, 5, 'b'))
        assert sum(not r['duplicate'] for r in results) == 1
        assert await first.get_balance(1, 2) == 125
        detail = await first.get_customer_detail(1, 2)
        assert (detail['deposit_total'],detail['withdraw_total'],detail['entry_count']) == (155,30,3)
    finally:
        await second.close(); await first.close()


async def test_cancelled_accounting_rolls_back_every_table(ledger_db, owner_id, customer_id, monkeypatch):
    async def cancelled(*args):
        raise asyncio.CancelledError()
    before = snapshot(ledger_db.db_path)
    monkeypatch.setattr(ledger_db, '_write_stats', cancelled)
    with pytest.raises(asyncio.CancelledError):
        await ledger_db.apply_amount(owner_id, customer_id, 1, 100, 'business-1')
    assert not ledger_db.conn.in_transaction
    assert_preserved(before, snapshot(ledger_db.db_path))


async def test_worker_cannot_send_uncommitted_rolled_back_receipt(ledger_db, owner_id, customer_id, monkeypatch):
    inserted, release = asyncio.Event(), asyncio.Event()
    original = ledger_db._insert_receipt_outbox
    outbox_id = None
    async def fail_after_insert(*args, **kwargs):
        nonlocal outbox_id
        outbox_id = await original(*args, **kwargs)
        inserted.set()
        await release.wait()
        raise RuntimeError('rollback fixture')
    monkeypatch.setattr(ledger_db, '_insert_receipt_outbox', fail_after_insert)
    accounting = asyncio.create_task(ledger_db.apply_amount(owner_id, customer_id, 1, 100, 'business-1'))
    await inserted.wait()
    ctx = context()
    delivery = asyncio.create_task(OutboxService(ledger_db).deliver_outbox_receipt(ctx, outbox_id))
    await asyncio.sleep(.01)
    ctx.bot.send_message.assert_not_awaited()
    release.set()
    with pytest.raises(RuntimeError, match='rollback fixture'):
        await accounting
    assert not await delivery
    ctx.bot.send_message.assert_not_awaited()
    assert await ledger_db.get_balance(owner_id, customer_id) == 0


async def test_two_workers_share_delivery_lock(ledger_db, owner_id, customer_id):
    record = await ledger_db.apply_amount(owner_id, customer_id, 1, 100, 'business-1')
    ctx = context()
    a,b = OutboxService(ledger_db),OutboxService(ledger_db)
    assert await asyncio.gather(a.deliver_outbox_receipt(ctx,record['outbox_id']),
                                b.deliver_outbox_receipt(ctx,record['outbox_id'])) == [True, True]
    ctx.bot.send_message.assert_awaited_once()
    ctx.bot.pin_chat_message.assert_awaited_once()


@pytest.mark.parametrize('retry_after', [60, timedelta(seconds=90)])
async def test_telegram_rate_limit_and_duplicate_do_not_bypass_cooldown(ledger_db, owner_id, customer_id, retry_after):
    record = await ledger_db.apply_amount(owner_id, customer_id, 1, 100, 'business-1')
    ctx = context(); ctx.bot.send_message.side_effect = RetryAfter(retry_after)
    service = OutboxService(ledger_db)
    before = datetime.now(timezone.utc)
    assert not await service.deliver_outbox_receipt(ctx,record['outbox_id'])
    row = await ledger_db.get_outbox(record['outbox_id'])
    seconds = retry_after.total_seconds() if isinstance(retry_after,timedelta) else retry_after
    assert (datetime.fromisoformat(row['next_attempt_at_utc'])-before).total_seconds() >= seconds-1
    ctx.bot.send_message.side_effect = None
    assert not await service.deliver_outbox_receipt(ctx,record['outbox_id'])
    ctx.bot.send_message.assert_awaited_once()
    assert await service.deliver_outbox_receipt(ctx,record['outbox_id'],
                                                now=datetime.fromisoformat(row['next_attempt_at_utc']))


async def test_sent_but_response_timeout_is_pending_not_exactly_once(ledger_db, owner_id, customer_id):
    record = await ledger_db.apply_amount(owner_id, customer_id, 1, 100, 'business-1')
    accepted = []
    async def ambiguous(**kwargs):
        accepted.append(kwargs['text'])
        raise TimedOut()
    ctx = context(); ctx.bot.send_message.side_effect = ambiguous
    assert not await OutboxService(ledger_db).deliver_outbox_receipt(ctx,record['outbox_id'])
    row = await ledger_db.get_outbox(record['outbox_id'])
    assert accepted == ['+10｜余10U'] and row['status']=='pending'
    assert await ledger_db.get_balance(owner_id,customer_id)==100
    ctx.bot.pin_chat_message.assert_not_awaited()


async def test_ack_failure_preserves_pending_without_reaccounting(ledger_db, owner_id, customer_id, monkeypatch):
    record = await ledger_db.apply_amount(owner_id, customer_id, 1, 100, 'business-1')
    monkeypatch.setattr(ledger_db,'mark_outbox_delivered',AsyncMock(side_effect=RuntimeError('disk fixture')))
    ctx = context()
    assert not await OutboxService(ledger_db).deliver_outbox_receipt(ctx,record['outbox_id'])
    assert (await ledger_db.get_outbox(record['outbox_id']))['status']=='pending'
    assert await ledger_db.get_balance(owner_id,customer_id)==100
    ctx.bot.send_message.assert_awaited_once()


async def test_start_and_orderly_shutdown_without_real_telegram(tmp_path):
    conf=settings(tmp_path/'lifecycle.db')
    runtime=Runtime(conf); application=build_application(conf,runtime=runtime)
    fake=SimpleNamespace(bot_data={},bot=SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=1,can_connect_to_business=True)),
        set_my_commands=AsyncMock()))
    await application.post_init(fake)
    assert runtime.store.conn is not None
    await asyncio.sleep(0)
    task=fake.bot_data['outbox_worker_task']
    await application.post_shutdown(fake)
    assert task.done() and runtime.store.conn is None


async def test_failed_startup_closes_database_and_starts_no_worker(tmp_path):
    conf=settings(tmp_path/'failed-start.db')
    runtime=Runtime(conf); application=build_application(conf,runtime=runtime)
    fake=SimpleNamespace(bot_data={},bot=SimpleNamespace(get_me=AsyncMock(side_effect=TimedOut())))
    with pytest.raises(TimedOut):
        await application.post_init(fake)
    assert runtime.store.conn is None and 'outbox_worker_task' not in fake.bot_data


def test_import_all_production_modules_has_no_runtime_side_effects(tmp_path):
    code = """
import socket, sqlite3, pathlib, importlib, pkgutil, jzbot
from unittest.mock import Mock
socket.socket.connect=Mock(side_effect=AssertionError('no network'))
sqlite3.connect=Mock(side_effect=AssertionError('no database'))
for item in pkgutil.walk_packages(jzbot.__path__,jzbot.__name__+'.'):
    importlib.import_module(item.name)
assert not list(pathlib.Path('.').glob('*.db'))
assert not list(pathlib.Path('.').glob('*.lock'))
assert not list(pathlib.Path('.').glob('*.log'))
print('IMPORT_OK')
"""
    import os
    env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    code = 'import sys; sys.path.insert(0,' + repr(str(Path(__file__).resolve().parents[1])) + ')\n' + code
    result=subprocess.run([sys.executable,'-c',code],cwd=tmp_path,env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='IMPORT_OK'


@pytest.mark.parametrize('problem', ['missing','relative','insecure','bad-token'])
def test_config_fails_closed(tmp_path, monkeypatch, problem):
    for name in ('JZBOT_CONFIG','JZBOT_DB_PATH','JZBOT_TOKEN','JZBOT_LOCK_PATH','JZBOT_LOG_PATH'):
        monkeypatch.delenv(name,raising=False)
    path=tmp_path/'ledger.db'; path.touch()
    monkeypatch.setenv('JZBOT_TOKEN',str(123456789)+":"+"X"*35)
    monkeypatch.setenv('JZBOT_DB_PATH',str(path))
    if problem=='missing': monkeypatch.setenv('JZBOT_DB_PATH',str(tmp_path/'absent.db'))
    if problem=='relative': monkeypatch.setenv('JZBOT_DB_PATH','ledger.db')
    if problem=='bad-token': monkeypatch.setenv('JZBOT_TOKEN','invalid-value')
    if problem=='insecure': monkeypatch.setenv('JZBOT_LOCK_PATH',str(path))
    with pytest.raises((ValueError,FileNotFoundError)):
        Settings.from_environment()


def test_formatter_redacts_exception_urls_and_config_repr(tmp_path):
    conf=settings(tmp_path/'db')
    assert conf.token not in repr(conf)
    record=logging.LogRecord('test',logging.ERROR,'',1,'failed %s',(conf.token,),None)
    value=RedactingFormatter(conf.token).format(record)
    assert conf.token not in value and '<redacted>' in value


@pytest.mark.parametrize('problem', ['extra-column','future-version'])
async def test_incompatible_db_stops_without_mutation(tmp_path, problem):
    path=tmp_path/'incompatible.db'
    first=LedgerStore(path); await first.open(); await first.close()
    c=sqlite3.connect(path)
    if problem=='extra-column': c.execute('ALTER TABLE ledger ADD COLUMN unexpected TEXT')
    else: c.execute("UPDATE app_meta SET meta_value='999' WHERE meta_key='jzbot_v2_schema'")
    c.commit(); c.close()
    old=path.read_bytes(); second=LedgerStore(path,require_existing=True)
    with pytest.raises(SchemaMismatch): await second.open()
    assert second.conn is None and path.read_bytes()==old


async def test_missing_production_db_never_created(tmp_path):
    path=tmp_path/'absent.db'; store=LedgerStore(path,require_existing=True)
    with pytest.raises(FileNotFoundError): await store.open()
    assert not path.exists() and store.conn is None


async def test_empty_existing_file_is_not_mistaken_for_production_ledger(tmp_path):
    path=tmp_path/'empty.db';path.touch()
    store=LedgerStore(path,require_existing=True)
    with pytest.raises(SchemaMismatch,match='no ledger'):
        await store.open()
    assert store.conn is None and path.stat().st_size==0


async def test_unreviewed_trigger_is_rejected_even_with_fixture_like_name(tmp_path):
    path=tmp_path/'trigger.db'
    store=LedgerStore(path)
    await store.open()
    await store.conn.execute('CREATE TRIGGER reject_fixture_unreviewed AFTER INSERT ON wallets BEGIN SELECT 1; END')
    await store.conn.commit()
    await store.close()
    before=path.read_bytes()
    with pytest.raises(SchemaMismatch,match='trigger'):
        await store.open()
    assert store.conn is None and path.read_bytes()==before


async def test_full_migration_rehearsal_compares_each_customer_and_every_record(ledger_db,owner_id,customer_id):
    for n in range(4):
        c=customer_id+n
        await ledger_db.upsert_customer_profile(owner_id,c,'fixture_user','合成','客户')
        await ledger_db.apply_amount(owner_id,c,1,10**100+5,'business-1')
        await ledger_db.apply_amount(owner_id,c,2,-20,'business-1')
        if n==0: await ledger_db.clear_balance(owner_id,c,3,'business-1')
        if n==1:
            req=await ledger_db.create_admin_zero(owner_id,owner_id,c)
            await ledger_db.resolve_admin_zero(owner_id,owner_id,req)
        if n==2: await ledger_db.set_customer_disabled(owner_id,owner_id,c,True)
    await ledger_db.conn.execute("DELETE FROM app_meta WHERE meta_key='jzbot_v2_schema'")
    await ledger_db.conn.commit(); path=ledger_db.db_path
    await ledger_db.close()
    before=snapshot(path)
    result=await rehearse(path)
    assert result['customer_scopes']==4
    assert_preserved(before,snapshot(path))
    # The comparator must detect offsetting mistakes, not only net totals.
    c=sqlite3.connect(path)
    c.execute('UPDATE wallets SET balance_tenths=? WHERE customer_chat_id=?',('1',customer_id))
    c.commit(); c.close()
    with pytest.raises(AssertionError): assert_preserved(before,snapshot(path))


async def test_historical_deposit_list_is_not_positive_balance_list(ledger_db,owner_id,customer_id):
    await ledger_db.apply_amount(owner_id,customer_id,1,100,'business-1')
    await ledger_db.clear_balance(owner_id,customer_id,2,'business-1')
    await ledger_db.apply_amount(owner_id,customer_id+1,1,-10,'business-1')
    rows=await ledger_db.get_deposit_wallet_rows(owner_id)
    assert [row['customer_chat_id'] for row in rows]==[customer_id]
    assert rows[0]['balance_tenths']=='0'
