"""Admin-only, offline state/zero tests. Every database is a pytest temporary DB."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import compat_api as jzbot
from test_admin_ui import add, buttons, callback_update, callbacks, check_view, rendered_query, snapshot


async def all_rows(database):
    names = await (await database.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )).fetchall()
    return {row['name']: [tuple(item) for item in await (
        await database.conn.execute(f"SELECT * FROM {row['name']} ORDER BY rowid")
    ).fetchall()] for row in names}


async def audit_rows(database):
    return await (await database.conn.execute("SELECT * FROM admin_operations ORDER BY id")).fetchall()


@pytest.mark.parametrize('disabled', [False, True])
async def test_internal_state_preserves_accounting_profiles_connections_and_search(
    ledger_db, owner_id, customer_id, disabled,
):
    await add(ledger_db, owner_id, customer_id, amount=150, name='客户<&测试')
    before = await all_rows(ledger_db)
    await ledger_db.set_customer_disabled(owner_id, owner_id, customer_id, disabled)
    after = await all_rows(ledger_db)
    assert {k: v for k, v in after.items() if k != 'customer_management'} == {
        k: v for k, v in before.items() if k != 'customer_management'}
    detail = await ledger_db.get_customer_detail(owner_id, customer_id)
    assert bool(detail['wallet']['is_disabled']) is disabled
    assert int(detail['wallet']['balance_tenths']) == 150
    assert detail['deposit_total'] == 150 and detail['entry_count'] == 1
    rows, total = await ledger_db.search_customers(owner_id, '测试')
    assert total == 1 and rows[0]['customer_chat_id'] == customer_id
    assert bool(rows[0]['is_disabled']) is disabled
    listed = await ledger_db.get_wallet_rows(owner_id)
    assert listed[0]['customer_chat_id'] == customer_id
    html, keyboard = jzbot.build_customer_detail_view(detail, 's', 0)
    assert ('已停用' if disabled else '正常') in html
    assert ('恢复正常' if disabled else '标记停用') in html
    assert '客户&lt;&amp;测试' in html
    assert f'state:{customer_id}:{int(not disabled)}:s:0' in callbacks(keyboard)
    check_view(html, keyboard)


async def test_disable_then_restore_does_not_restore_old_money(ledger_db, owner_id, customer_id):
    await add(ledger_db, owner_id, customer_id, amount=150)
    await ledger_db.set_customer_disabled(owner_id, owner_id, customer_id, True)
    request = await ledger_db.create_admin_zero(owner_id, owner_id, customer_id)
    await ledger_db.resolve_admin_zero(owner_id, owner_id, request)
    await ledger_db.set_customer_disabled(owner_id, owner_id, customer_id, False)
    assert await ledger_db.get_balance(owner_id, customer_id) == 0
    # Management status deliberately does NOT block later normal Business accounting.
    await ledger_db.set_customer_disabled(owner_id, owner_id, customer_id, True)
    result = await ledger_db.apply_amount(owner_id, customer_id, 2, 5, 'business-1')
    assert result['receipt_text'] == '+0.5｜余0.5U'


@pytest.mark.parametrize('balance', [150, -80, 10**100, -(10**100), 0])
@pytest.mark.parametrize('disabled', [False, True])
async def test_admin_zero_atomic_audit_history_stats_and_no_customer_outbox(
    ledger_db, owner_id, customer_id, balance, disabled,
):
    if balance:
        await add(ledger_db, owner_id, customer_id, amount=balance)
    else:
        await add(ledger_db, owner_id, customer_id, source=1, amount=5)
        await add(ledger_db, owner_id, customer_id, source=2, amount=-5)
    await ledger_db.set_customer_disabled(owner_id, owner_id, customer_id, disabled)
    detail_before = await ledger_db.get_customer_detail(owner_id, customer_id)
    before = await all_rows(ledger_db)
    request = await ledger_db.create_admin_zero(owner_id, owner_id, customer_id)
    assert await all_rows(ledger_db) == before  # confirmation display is read-only
    result, duplicate = await ledger_db.resolve_admin_zero(owner_id, owner_id, request)
    assert not duplicate and result['result'] == ('applied' if balance else 'noop')
    assert int(result['balance_before']) == balance and int(result['balance_after']) == 0
    assert result['operation_id'] == request['operation_id']
    assert result['owner_user_id'] == owner_id == result['administrator_id']
    assert result['customer_chat_id'] == customer_id
    assert result['operation_type'] == 'admin_zero' and result['entry_type'] == 'clear'
    assert result['created_at_utc'] and result['completed_at_utc']
    after = await all_rows(ledger_db)
    for table in ('ledger', 'processed_messages', 'receipt_outbox', 'customer_profiles', 'business_connections'):
        assert after[table] == before[table]
    detail = await ledger_db.get_customer_detail(owner_id, customer_id)
    assert int(detail['wallet']['balance_tenths']) == 0
    assert bool(detail['wallet']['is_disabled']) is disabled
    assert detail['deposit_total'] == detail_before['deposit_total']
    assert detail['withdraw_total'] == detail_before['withdraw_total']
    assert detail['entry_count'] == detail_before['entry_count'] + bool(balance)
    entries, total, page = await ledger_db.get_ledger_page(owner_id, customer_id)
    assert total == detail['entry_count'] and page == 0
    if balance:
        clear = entries[0]
        assert clear['entry_type'] == 'clear' and clear['operation_origin'] == 'admin'
        assert clear['source_message_id'] is None  # never a forged Telegram ID
        assert clear['operation_id'] == request['operation_id']
        assert int(clear['balance_before']) == balance and int(clear['balance_after']) == 0
        assert int(clear['amount_tenths']) == abs(balance)
        assert '管理员归零' in jzbot.admin_entry_line(clear)
    else:
        assert all(row['entry_type'] != 'clear' for row in entries)


async def test_double_confirmation_and_concurrent_requests_are_idempotent(ledger_db, owner_id, customer_id):
    await add(ledger_db, owner_id, customer_id, amount=150)
    first = await ledger_db.create_admin_zero(owner_id, owner_id, customer_id)
    second = await ledger_db.create_admin_zero(owner_id, owner_id, customer_id)
    results = await asyncio.gather(
        ledger_db.resolve_admin_zero(owner_id, owner_id, first),
        ledger_db.resolve_admin_zero(owner_id, owner_id, first),
        ledger_db.resolve_admin_zero(owner_id, owner_id, second),
    )
    assert sum(not duplicate and row['result'] == 'applied' for row, duplicate in results) == 1
    assert results[1][1] is True
    assert results[2][0]['result'] == 'stale'
    assert len(await audit_rows(ledger_db)) == 2
    assert await ledger_db.get_balance(owner_id, customer_id) == 0
    assert (await ledger_db.get_customer_detail(owner_id, customer_id))['entry_count'] == 2


async def test_cross_connection_concurrency_is_protected_by_sqlite(tmp_path, monkeypatch, owner_id, customer_id):
    first = jzbot.LedgerDB(tmp_path / 'concurrent.db')
    second = jzbot.LedgerDB(tmp_path / 'concurrent.db')
    await first.open()
    await first.upsert_business_connection('business-1', owner_id, True)
    await add(first, owner_id, customer_id, amount=150)
    await second.open()
    try:
        request = await first.create_admin_zero(owner_id, owner_id, customer_id)
        results = await asyncio.gather(
            first.resolve_admin_zero(owner_id, owner_id, request),
            second.resolve_admin_zero(owner_id, owner_id, request),
        )
        assert sorted(duplicate for _, duplicate in results) == [False, True]
        assert len(await audit_rows(first)) == 1
        assert (await first.get_customer_detail(owner_id, customer_id))['entry_count'] == 2
    finally:
        await second.close()
        await first.close()


@pytest.mark.parametrize('round_trip', [False, True])
async def test_balance_change_or_return_to_same_balance_invalidates_confirmation(
    ledger_db, owner_id, customer_id, round_trip,
):
    await add(ledger_db, owner_id, customer_id, amount=150)
    request = await ledger_db.create_admin_zero(owner_id, owner_id, customer_id)
    await add(ledger_db, owner_id, customer_id, source=2, amount=5)
    if round_trip:
        await add(ledger_db, owner_id, customer_id, source=3, amount=-5)
    before = await snapshot(ledger_db)
    result, _ = await ledger_db.resolve_admin_zero(owner_id, owner_id, request)
    assert result['result'] == 'stale'
    assert await snapshot(ledger_db) == before


async def test_expired_confirmation_is_audited_but_never_clears(ledger_db, owner_id, customer_id):
    await add(ledger_db, owner_id, customer_id, amount=150)
    now = datetime.now(timezone.utc)
    request = await ledger_db.create_admin_zero(owner_id, owner_id, customer_id, now=now)
    before = await snapshot(ledger_db)
    result, _ = await ledger_db.resolve_admin_zero(owner_id, owner_id, request, now=now+timedelta(minutes=5))
    assert result['result'] == 'expired'
    assert await snapshot(ledger_db) == before


@pytest.mark.parametrize('mode', ['other_actor', 'other_owner', 'missing_customer'])
async def test_database_layer_owner_authorization(ledger_db, owner_id, customer_id, mode):
    await add(ledger_db, owner_id, customer_id, amount=150)
    await ledger_db.upsert_business_connection('other', owner_id+1, True)
    request = await ledger_db.create_admin_zero(owner_id, owner_id, customer_id)
    before = await all_rows(ledger_db)
    owner, actor, customer = owner_id, owner_id, customer_id
    if mode == 'other_actor':
        actor += 1
    elif mode == 'other_owner':
        owner += 1
        actor = owner
    else:
        customer += 1
    with pytest.raises(PermissionError):
        await ledger_db.set_customer_disabled(owner, actor, customer, True)
    with pytest.raises(PermissionError):
        await ledger_db.create_admin_zero(owner, actor, customer)
    if mode != 'missing_customer':
        with pytest.raises(PermissionError):
            await ledger_db.resolve_admin_zero(owner, actor, request)
    assert await all_rows(ledger_db) == before


async def test_admin_zero_rollback_includes_balance_stats_and_ledger(ledger_db, monkeypatch, owner_id, customer_id):
    await add(ledger_db, owner_id, customer_id, amount=150)
    request = await ledger_db.create_admin_zero(owner_id, owner_id, customer_id)
    before = await snapshot(ledger_db)
    original = ledger_db._write_stats
    async def fail_after_stats(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError('fixture transaction failure')
    monkeypatch.setattr(ledger_db, '_write_stats', fail_after_stats)
    with pytest.raises(RuntimeError, match='fixture transaction failure'):
        await ledger_db.resolve_admin_zero(owner_id, owner_id, request)
    assert await snapshot(ledger_db) == before
    rows = await audit_rows(ledger_db)
    assert len(rows) == 1 and rows[0]['result'] == 'failed'
    assert rows[0]['balance_before'] == rows[0]['balance_after'] == '150'
    monkeypatch.setattr(ledger_db, '_write_stats', original)
    result, duplicate = await ledger_db.resolve_admin_zero(owner_id, owner_id, request)
    assert duplicate and result['result'] == 'failed'


async def test_failure_at_clear_audit_write_rolls_back_all_money(ledger_db, owner_id, customer_id):
    await add(ledger_db, owner_id, customer_id, amount=150)
    request = await ledger_db.create_admin_zero(owner_id, owner_id, customer_id)
    before = await snapshot(ledger_db)
    await ledger_db.conn.execute("""
        CREATE TEMP TRIGGER reject_fixture_admin_clear BEFORE UPDATE OF result ON admin_operations
        WHEN NEW.result='applied' BEGIN SELECT RAISE(ABORT, 'fixture clear ledger failure'); END
    """)
    await ledger_db.conn.commit()
    with pytest.raises(Exception, match='fixture clear ledger failure'):
        await ledger_db.resolve_admin_zero(owner_id, owner_id, request)
    assert await snapshot(ledger_db) == before
    assert (await audit_rows(ledger_db))[0]['result'] == 'failed'
    assert not await ledger_db._admin_clear_count(owner_id, customer_id)


async def test_repeated_zero_balance_requests_create_audits_not_fictitious_history(ledger_db, owner_id, customer_id):
    await add(ledger_db, owner_id, customer_id, amount=5)
    await add(ledger_db, owner_id, customer_id, source=2, amount=-5)
    before = await snapshot(ledger_db)
    requests = [await ledger_db.create_admin_zero(owner_id, owner_id, customer_id) for _ in range(3)]
    for request in requests:
        result, _ = await ledger_db.resolve_admin_zero(owner_id, owner_id, request)
        assert result['result'] == 'noop'
    assert await snapshot(ledger_db) == before
    assert len(await audit_rows(ledger_db)) == 3


async def test_mixed_clear_sources_and_history_pagination_are_stably_ordered(ledger_db, owner_id, customer_id):
    await add(ledger_db, owner_id, customer_id, amount=150)
    await ledger_db.clear_balance(owner_id, customer_id, 2, 'business-1')
    for index in range(3, 19):
        await add(ledger_db, owner_id, customer_id, source=index, amount=5)
        request = await ledger_db.create_admin_zero(owner_id, owner_id, customer_id)
        await ledger_db.resolve_admin_zero(owner_id, owner_id, request)
    before = await all_rows(ledger_db)
    seen = []
    for page in range(4):
        rows, total, actual = await ledger_db.get_ledger_page(owner_id, customer_id, page)
        assert total == 34 and actual == page and len(rows) <= 10
        seen.extend(rows)
    assert [row['operation_origin'] for row in seen[:32]] == ['admin','business'] * 16
    assert '账本已清空' in jzbot.admin_entry_line(seen[-2])
    assert seen[-2]['source_message_id'] == 2
    assert seen[-2]['operation_origin'] == 'business'
    assert len({row['operation_id'] for row in seen if row['operation_origin']=='admin'}) == 16
    recent = await ledger_db.get_recent_entries(owner_id, customer_id, oldest_first=False)
    assert [tuple(row) for row in recent] == [tuple(row) for row in seen[:5]]
    assert await all_rows(ledger_db) == before


async def test_other_owner_does_not_see_admin_clear_or_status(ledger_db, owner_id, customer_id):
    await ledger_db.upsert_business_connection('other', owner_id+1, True)
    await add(ledger_db, owner_id, customer_id, amount=150)
    await add(ledger_db, owner_id+1, customer_id, amount=30)
    await ledger_db.set_customer_disabled(owner_id, owner_id, customer_id, True)
    await ledger_db.resolve_admin_zero(owner_id, owner_id,
        await ledger_db.create_admin_zero(owner_id, owner_id, customer_id))
    other = await ledger_db.get_customer_detail(owner_id+1, customer_id)
    assert int(other['wallet']['balance_tenths']) == 30
    assert not other['wallet']['is_disabled']
    rows, total, _ = await ledger_db.get_ledger_page(owner_id+1, customer_id)
    assert total == 1 and rows[0]['entry_type'] == 'deposit'


async def test_ui_cancel_is_completely_readonly_and_old_nonce_is_rejected(ledger_db, monkeypatch, owner_id, customer_id):
    await add(ledger_db, owner_id, customer_id, amount=150, name='客户<&甲')
    monkeypatch.setattr(jzbot, 'db', ledger_db)
    context = SimpleNamespace(user_data={'customer_search_query':'甲'})
    before = await all_rows(ledger_db)
    open_update = callback_update(f'zero:{customer_id}:s:2', owner_id)
    await jzbot.on_admin_callback(open_update, context)
    html = rendered_query(open_update.callback_query)['text']
    assert '客户&lt;&amp;甲' in html and str(customer_id) in html
    assert '确认归零' in html and '取消操作' in html and '15 U' in html
    nonce = next(iter(context.user_data['admin_zero_requests']))
    cancelled = callback_update('zero_cancel:'+nonce, owner_id)
    await jzbot.on_admin_callback(cancelled, context)
    assert '已取消' in rendered_query(cancelled.callback_query)['text']
    assert await all_rows(ledger_db) == before
    stale = callback_update('zero_confirm:'+nonce, owner_id)
    await jzbot.on_admin_callback(stale, context)
    assert '失效' in stale.callback_query.answer.await_args.args[0]
    assert await all_rows(ledger_db) == before
    assert context.user_data['customer_search_query'] == '甲'


async def test_ui_zero_does_not_contact_customer_or_pin(ledger_db, monkeypatch, owner_id, customer_id):
    await add(ledger_db, owner_id, customer_id, amount=150)
    monkeypatch.setattr(jzbot, 'db', ledger_db)
    context = SimpleNamespace(user_data={}, bot=SimpleNamespace(
        send_message=AsyncMock(side_effect=AssertionError('no customer messages')),
        pin_chat_message=AsyncMock(side_effect=AssertionError('no customer pin')),
        get_business_connection=AsyncMock(side_effect=AssertionError('no Business API dependency')),
    ))
    await jzbot.on_admin_callback(callback_update(f'zero:{customer_id}:a:0', owner_id), context)
    nonce = next(iter(context.user_data['admin_zero_requests']))
    confirm = callback_update('zero_confirm:'+nonce, owner_id)
    await jzbot.on_admin_callback(confirm, context)
    html = rendered_query(confirm.callback_query)['text']
    assert '归零已完成' in html and '<b>0 U</b>' in html and '管理员归零' in html
    confirm.callback_query.answer.assert_awaited_once()
    assert confirm.callback_query.get_bot()._post.await_args.args[0] == 'editMessageText'
    context.bot.send_message.assert_not_awaited()
    context.bot.pin_chat_message.assert_not_awaited()
    context.bot.get_business_connection.assert_not_awaited()
    again = callback_update('zero_confirm:'+nonce, owner_id)
    await jzbot.on_admin_callback(again, context)
    assert len(await audit_rows(ledger_db)) == 1
    assert '已处理' in rendered_query(again.callback_query)['text']


@pytest.mark.parametrize('data', ['state:2001:2:a:0','zero:bad:a:0','zero_confirm:forged',
                                 'zero_cancel:forged', 'state:2001:1:a:-1'])
async def test_forged_or_malformed_management_callbacks_never_write(ledger_db, monkeypatch, owner_id, data):
    monkeypatch.setattr(jzbot, 'db', ledger_db)
    before = await all_rows(ledger_db)
    update = callback_update(data, owner_id)
    await jzbot.on_admin_callback(update, SimpleNamespace(user_data={}))
    update.callback_query.answer.assert_awaited_once()
    assert await all_rows(ledger_db) == before


async def test_management_requires_private_chat_and_current_owner(ledger_db, monkeypatch, owner_id, customer_id):
    await add(ledger_db, owner_id, customer_id, amount=150)
    monkeypatch.setattr(jzbot, 'db', ledger_db)
    before = await all_rows(ledger_db)
    wrong_chat = callback_update(f'zero:{customer_id}:a:0', owner_id)
    wrong_chat.callback_query.message.chat_id = -100
    await jzbot.on_admin_callback(wrong_chat, SimpleNamespace(user_data={}))
    assert '私聊' in wrong_chat.callback_query.answer.await_args.args[0]
    outsider = callback_update(f'state:{customer_id}:1:a:0', owner_id+1)
    await jzbot.on_admin_callback(outsider, SimpleNamespace(user_data={}))
    assert '无权' in outsider.callback_query.answer.await_args.args[0]
    assert await all_rows(ledger_db) == before


async def test_state_buttons_refresh_without_accounting_side_effects(ledger_db, monkeypatch, owner_id, customer_id):
    await add(ledger_db, owner_id, customer_id, amount=150)
    monkeypatch.setattr(jzbot, 'db', ledger_db)
    context = SimpleNamespace(user_data={})
    before = await snapshot(ledger_db)
    for disabled in (True, False):
        update = callback_update(f'state:{customer_id}:{int(disabled)}:a:0', owner_id)
        await jzbot.on_admin_callback(update, context)
        rendered = rendered_query(update.callback_query)
        assert ('恢复正常' if disabled else '标记停用') in rendered['text']
        assert ('已标记停用，仅为内部账本状态' if disabled else '已恢复正常') in rendered['text']
        assert await snapshot(ledger_db) == before
        update.callback_query.answer.assert_awaited_once()


@pytest.mark.parametrize('data_prefix', ['zero', 'state'])
async def test_forged_other_customer_callback_is_rejected(ledger_db, monkeypatch, owner_id, customer_id, data_prefix):
    await ledger_db.upsert_business_connection('other', owner_id+1, True)
    await add(ledger_db, owner_id+1, customer_id, amount=150)
    monkeypatch.setattr(jzbot, 'db', ledger_db)
    before = await all_rows(ledger_db)
    data = f'zero:{customer_id}:a:0' if data_prefix == 'zero' else f'state:{customer_id}:1:a:0'
    update = callback_update(data, owner_id)
    await jzbot.on_admin_callback(update, SimpleNamespace(user_data={}))
    assert '无权' in update.callback_query.answer.await_args.args[0]
    assert await all_rows(ledger_db) == before


async def test_confirmation_does_not_survive_restart_or_lost_context(ledger_db, monkeypatch, owner_id, customer_id):
    await add(ledger_db, owner_id, customer_id, amount=150)
    monkeypatch.setattr(jzbot, 'db', ledger_db)
    context = SimpleNamespace(user_data={})
    await jzbot.on_admin_callback(callback_update(f'zero:{customer_id}:a:0', owner_id), context)
    nonce = next(iter(context.user_data['admin_zero_requests']))
    before = await all_rows(ledger_db)
    update = callback_update('zero_confirm:'+nonce, owner_id)
    await jzbot.on_admin_callback(update, SimpleNamespace(user_data={}))
    assert '失效' in update.callback_query.answer.await_args.args[0]
    assert await all_rows(ledger_db) == before


async def test_db_and_handler_recheck_owner_after_confirmation(ledger_db, monkeypatch, owner_id, customer_id):
    await add(ledger_db, owner_id, customer_id, amount=150)
    request = await ledger_db.create_admin_zero(owner_id, owner_id, customer_id)
    monkeypatch.setattr(jzbot, 'db', ledger_db)
    # Only the fixture's temporary DB is changed to simulate revoked ownership.
    await ledger_db.conn.execute('DELETE FROM business_accounts WHERE owner_user_id=?', (owner_id,))
    await ledger_db.conn.commit()
    before = await all_rows(ledger_db)
    with pytest.raises(PermissionError):
        await ledger_db.resolve_admin_zero(owner_id, owner_id, request)
    context = SimpleNamespace(user_data={'admin_zero_requests':{request['operation_id']:request}})
    update = callback_update('zero_confirm:'+request['operation_id'], owner_id)
    await jzbot.on_admin_callback(update, context)
    assert '无权' in update.callback_query.answer.await_args.args[0]
    assert await all_rows(ledger_db) == before


async def test_zero_works_without_any_enabled_business_connection(ledger_db, owner_id, customer_id):
    await add(ledger_db, owner_id, customer_id, amount=150)
    await ledger_db.mark_business_connection_disabled('business-1', owner_id)
    assert not await ledger_db.get_enabled_connection_ids(owner_id)
    request = await ledger_db.create_admin_zero(owner_id, owner_id, customer_id)
    result, _ = await ledger_db.resolve_admin_zero(owner_id, owner_id, request)
    assert result['result'] == 'applied'
    assert await ledger_db.get_balance(owner_id, customer_id) == 0


@pytest.mark.parametrize('failure', ['expired', 'stale'])
async def test_ui_expired_or_changed_balance_returns_detail_without_clearing(
    ledger_db, monkeypatch, owner_id, customer_id, failure,
):
    await add(ledger_db, owner_id, customer_id, amount=150)
    monkeypatch.setattr(jzbot, 'db', ledger_db)
    context = SimpleNamespace(user_data={})
    await jzbot.on_admin_callback(callback_update(f'zero:{customer_id}:a:0', owner_id), context)
    nonce = next(iter(context.user_data['admin_zero_requests']))
    if failure == 'expired':
        context.user_data['admin_zero_requests'][nonce]['expires_at_utc'] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    else:
        await add(ledger_db, owner_id, customer_id, source=2, amount=5)
    before = await snapshot(ledger_db)
    update = callback_update('zero_confirm:'+nonce, owner_id)
    await jzbot.on_admin_callback(update, context)
    html = rendered_query(update.callback_query)['text']
    assert ('已过期' if failure == 'expired' else '已变化') in html
    assert await snapshot(ledger_db) == before
    assert (await audit_rows(ledger_db))[0]['result'] == failure


async def test_state_and_admin_history_persist_across_idempotent_reopen(ledger_db, owner_id, customer_id):
    await add(ledger_db, owner_id, customer_id, amount=150)
    await ledger_db.set_customer_disabled(owner_id, owner_id, customer_id, True)
    request = await ledger_db.create_admin_zero(owner_id, owner_id, customer_id)
    await ledger_db.resolve_admin_zero(owner_id, owner_id, request)
    before = await all_rows(ledger_db)
    await ledger_db.close()
    await ledger_db.open()
    assert await all_rows(ledger_db) == before
    result, duplicate = await ledger_db.resolve_admin_zero(owner_id, owner_id, request)
    assert duplicate and result['result'] == 'applied'
    detail = await ledger_db.get_customer_detail(owner_id, customer_id)
    assert detail['wallet']['is_disabled'] == 1
    assert '管理员归零' in jzbot.admin_entry_line(detail['recent'][0])


async def test_regular_clear_outbox_and_stats_after_admin_zero_stay_independent(ledger_db, owner_id, customer_id):
    await add(ledger_db, owner_id, customer_id, amount=150)
    request = await ledger_db.create_admin_zero(owner_id, owner_id, customer_id)
    await ledger_db.resolve_admin_zero(owner_id, owner_id, request)
    noop = await ledger_db.clear_balance(owner_id, customer_id, 2, 'business-1')
    assert not noop['changed'] and noop['receipt_text'] == '余额已清零'
    assert (await ledger_db.get_outbox(noop['outbox_id']))['should_pin'] == 0
    await add(ledger_db, owner_id, customer_id, source=3, amount=5)
    cleared = await ledger_db.clear_balance(owner_id, customer_id, 4, 'business-1')
    row = await ledger_db.get_outbox(cleared['outbox_id'])
    assert row['receipt_text'] == '余额已清零' and row['reply_to_message_id'] == 4
    assert row['should_pin'] == 1
    detail = await ledger_db.get_customer_detail(owner_id, customer_id)
    assert detail['entry_count'] == 4 and detail['deposit_total'] == 155
    assert detail['withdraw_total'] == 0
    entries, total, _ = await ledger_db.get_ledger_page(owner_id, customer_id)
    assert total == 4
    assert [row['operation_origin'] for row in entries] == ['business','business','admin','business']


async def test_combined_history_is_indexed_and_sql_limited(ledger_db, owner_id, customer_id):
    await add(ledger_db, owner_id, customer_id, amount=150)
    request = await ledger_db.create_admin_zero(owner_id, owner_id, customer_id)
    await ledger_db.resolve_admin_zero(owner_id, owner_id, request)
    statements = []
    await ledger_db.conn.set_trace_callback(statements.append)
    await ledger_db.get_ledger_page(owner_id, customer_id)
    query = next(sql for sql in statements if 'WITH business AS' in sql)
    assert query.count('LIMIT') == 3 and 'OFFSET 0' in query
    plan = await (await ledger_db.conn.execute('EXPLAIN QUERY PLAN '+query)).fetchall()
    assert any('idx_ledger_recent' in str(tuple(row)) for row in plan)
    assert any('idx_admin_clear_history' in str(tuple(row)) for row in plan)


async def test_confirmation_cache_is_bounded_and_eviction_is_readonly(ledger_db, monkeypatch, owner_id, customer_id):
    await add(ledger_db, owner_id, customer_id, amount=150)
    monkeypatch.setattr(jzbot, 'db', ledger_db)
    before = await all_rows(ledger_db)
    context = SimpleNamespace(user_data={})
    first = None
    for _ in range(18):
        await jzbot.on_admin_callback(callback_update(f'zero:{customer_id}:a:0', owner_id), context)
        if first is None:
            first = next(iter(context.user_data['admin_zero_requests']))
    assert len(context.user_data['admin_zero_requests']) == 16
    assert first not in context.user_data['admin_zero_requests']
    assert await all_rows(ledger_db) == before


async def test_home_remains_truthful_about_disabled_money(ledger_db, owner_id, customer_id):
    await add(ledger_db, owner_id, customer_id, amount=-150)
    await ledger_db.set_customer_disabled(owner_id, owner_id, customer_id, True)
    text, keyboard = jzbot.build_home_view(await ledger_db.get_wallet_rows(owner_id))
    assert '<b>-15 U</b>' in text and '金额仍计入统计' in text
    assert '已标记停用' in text
    check_view(text, keyboard)


async def test_additive_migration_is_idempotent_and_preserves_old_data(ledger_db, owner_id, customer_id):
    await add(ledger_db, owner_id, customer_id, amount=150)
    old = await snapshot(ledger_db)
    original_schema = (await (await ledger_db.conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='ledger'"
    )).fetchone())['sql']
    # Simulate a pre-feature database using this test's TEMPORARY file only.
    await ledger_db.conn.execute('DROP TABLE admin_operations')
    await ledger_db.conn.execute('DROP TABLE customer_management')
    await ledger_db.conn.commit()
    await ledger_db._ensure_customer_management_schema()
    first = await all_rows(ledger_db)
    await ledger_db._ensure_customer_management_schema()
    assert await all_rows(ledger_db) == first
    assert await snapshot(ledger_db) == old
    assert (await (await ledger_db.conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='ledger'"
    )).fetchone())['sql'] == original_schema
    assert (await (await ledger_db.conn.execute('PRAGMA quick_check')).fetchone())[0] == 'ok'
    assert (await (await ledger_db.conn.execute('PRAGMA journal_mode')).fetchone())[0] == 'wal'
