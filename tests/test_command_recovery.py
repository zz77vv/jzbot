"""Trusted ingress survives rollback, restart and ambiguous queue acknowledgements."""

import asyncio
import contextlib
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_v2_business import context, update
from test_v2_core import settings

from jzbot.app import build_application
from jzbot.runtime import Runtime


def fail_stats(*args, **kwargs):
    raise sqlite3.OperationalError("synthetic storage failure")


async def rows(store, table):
    return await (await store.conn.execute(f"SELECT * FROM {table}")).fetchall()


async def test_transient_rollback_is_retained_then_committed_once(
    ledger_db, owner_id, customer_id, monkeypatch
):
    rt = Runtime(settings(ledger_db.db_path), store=ledger_db)
    ctx = context()
    original = ledger_db._write_stats
    monkeypatch.setattr(ledger_db, "_write_stats", AsyncMock(side_effect=fail_stats))
    await rt.business.on_business_message(update(owner_id, customer_id), ctx)
    assert await ledger_db.get_balance(owner_id, customer_id) == 0
    assert not await rows(ledger_db, "processed_messages")
    ctx.bot.send_message.assert_not_awaited()
    await rt.commands.process_notices(ctx)
    assert ctx.bot.send_message.await_count == 1
    assert ctx.bot.send_message.await_args.kwargs["chat_id"] == owner_id
    monkeypatch.setattr(ledger_db, "_write_stats", original)
    assert await rt.commands.process_one(ctx, now=10**12)
    assert await ledger_db.get_balance(owner_id, customer_id) == 150
    assert len(await rows(ledger_db, "ledger")) == 1
    assert len(await rows(ledger_db, "receipt_outbox")) == 1
    assert (await rt.commands.queue.get(owner_id, customer_id, 1))["status"] == "done"
    await rt.commands.process_notices(ctx)
    assert "已确认入账" in ctx.bot.send_message.await_args.kwargs["text"]


async def test_persistent_failure_is_retained_owner_notified_once_and_backoff_bounded(
    ledger_db, owner_id, customer_id, monkeypatch
):
    rt = Runtime(settings(ledger_db.db_path), store=ledger_db)
    ctx = context()
    monkeypatch.setattr(ledger_db, "_write_stats", AsyncMock(side_effect=fail_stats))
    await rt.business.on_business_message(update(owner_id, customer_id), ctx)
    await rt.commands.process_notices(ctx)
    assert ctx.bot.send_message.await_count == 1
    for attempt in range(12):
        now = 10**12 + attempt * 1000
        assert await rt.commands.process_one(ctx, now=now)
        row = await rt.commands.queue.get(owner_id, customer_id, 1)
        assert row["status"] == "pending"
        assert 1 <= row["due"] - now <= 300
        await rt.commands.process_notices(ctx, now=now)
    assert ctx.bot.send_message.await_count == 1
    assert not await rows(ledger_db, "ledger")


async def test_restart_recovers_pending_command_and_original_source(
    ledger_db, owner_id, customer_id, monkeypatch
):
    conf = settings(ledger_db.db_path)
    first = Runtime(conf, store=ledger_db)
    ctx = context()
    original = ledger_db._write_stats
    monkeypatch.setattr(ledger_db, "_write_stats", AsyncMock(side_effect=fail_stats))
    await first.business.on_business_message(update(owner_id, customer_id, source=42), ctx)
    await first.commands.process_notices(ctx)
    assert ctx.bot.send_message.await_count == 1
    await first.commands.queue.close()
    monkeypatch.setattr(ledger_db, "_write_stats", original)
    restarted = Runtime(conf, store=ledger_db)
    assert await restarted.commands.process_one(ctx, now=10**12)
    assert await ledger_db.get_balance(owner_id, customer_id) == 150
    assert [r["source_message_id"] for r in await rows(ledger_db, "ledger")] == [42]


async def test_same_customer_clear_waits_for_pending_amount(
    ledger_db, owner_id, customer_id, monkeypatch
):
    rt = Runtime(settings(ledger_db.db_path), store=ledger_db)
    ctx = context()
    original = ledger_db._write_stats
    monkeypatch.setattr(ledger_db, "_write_stats", AsyncMock(side_effect=fail_stats))
    await rt.business.on_business_message(update(owner_id, customer_id, "+15", 1), ctx)
    monkeypatch.setattr(ledger_db, "_write_stats", original)
    await rt.business.on_business_message(update(owner_id, customer_id, "//", 2), ctx)
    assert not await rows(ledger_db, "processed_messages")
    assert await rt.commands.process_one(ctx, now=10**12)
    assert await ledger_db.get_balance(owner_id, customer_id) == 150
    assert await rt.commands.process_one(ctx, now=10**12)
    assert await ledger_db.get_balance(owner_id, customer_id) == 0
    entries = await rows(ledger_db, "ledger")
    assert [r["entry_type"] for r in entries] == ["deposit", "clear"]
    assert [r["source_message_id"] for r in entries] == [1, 2]


async def test_queue_ack_failure_replays_without_duplicate_credit(
    ledger_db, owner_id, customer_id, monkeypatch
):
    rt = Runtime(settings(ledger_db.db_path), store=ledger_db)
    ctx = context()
    assert hasattr(rt, "commands"), "Trusted commands need independent recovery"
    original = rt.commands.queue.complete
    monkeypatch.setattr(rt.commands.queue, "complete", AsyncMock(side_effect=fail_stats))
    await rt.business.on_business_message(update(owner_id, customer_id), ctx)
    assert await ledger_db.get_balance(owner_id, customer_id) == 150
    assert (await rt.commands.queue.get(owner_id, customer_id, 1))["status"] == "pending"
    await rt.commands.process_notices(ctx)
    assert "回滚" not in ctx.bot.send_message.await_args.kwargs["text"]
    monkeypatch.setattr(rt.commands.queue, "complete", original)
    assert await rt.commands.process_one(ctx, now=10**12)
    await rt.business.on_business_message(update(owner_id, customer_id), ctx)
    assert await ledger_db.get_balance(owner_id, customer_id) == 150
    for table in ("ledger", "processed_messages", "receipt_outbox"):
        assert len(await rows(ledger_db, table)) == 1


async def test_owner_and_customer_isolation_and_reauthorization(
    ledger_db, owner_id, customer_id, monkeypatch
):
    rt = Runtime(settings(ledger_db.db_path), store=ledger_db)
    ctx = context()
    original = ledger_db._write_stats
    monkeypatch.setattr(ledger_db, "_write_stats", AsyncMock(side_effect=fail_stats))
    await rt.business.on_business_message(update(owner_id, customer_id), ctx)
    await rt.commands.process_notices(ctx)
    assert ctx.bot.send_message.await_count == 1
    monkeypatch.setattr(ledger_db, "_write_stats", original)
    await rt.business.on_business_message(update(owner_id, customer_id + 1), ctx)
    assert await ledger_db.get_balance(owner_id, customer_id + 1) == 150
    await ledger_db.upsert_business_connection("other", owner_id + 1, True)
    other = update(owner_id + 1, customer_id)
    other.business_message.business_connection_id = "other"
    await rt.business.on_business_message(other, ctx)
    assert await ledger_db.get_balance(owner_id + 1, customer_id) == 150
    assert await rt.commands.queue.get(owner_id + 1, customer_id + 1, 1) is None
    await ledger_db.mark_business_connection_disabled("business-1", owner_id)
    assert await rt.commands.process_one(ctx, now=10**12)
    assert await ledger_db.get_balance(owner_id, customer_id) == 0
    assert (await rt.commands.queue.get(owner_id, customer_id, 1))["status"] == "pending"


async def test_enqueue_failure_alerts_owner_and_never_mutates_ledger(
    ledger_db, owner_id, customer_id, monkeypatch
):
    rt = Runtime(settings(ledger_db.db_path), store=ledger_db)
    ctx = context()
    assert hasattr(rt, "commands"), "Trusted commands must persist before accounting"
    monkeypatch.setattr(rt.commands.queue, "enqueue", AsyncMock(side_effect=fail_stats))
    await rt.business.on_business_message(update(owner_id, customer_id), ctx)
    assert not await rows(ledger_db, "ledger")
    assert ctx.bot.send_message.await_args.kwargs["chat_id"] == owner_id
    assert "未确认" in ctx.bot.send_message.await_args.kwargs["text"]


async def test_failed_owner_notice_is_retried_after_restart(
    ledger_db, owner_id, customer_id, monkeypatch
):
    conf = settings(ledger_db.db_path)
    rt = Runtime(conf, store=ledger_db)
    ctx = context()
    ctx.bot.send_message.side_effect = RuntimeError("offline fixture")
    monkeypatch.setattr(ledger_db, "_write_stats", AsyncMock(side_effect=fail_stats))
    await rt.business.on_business_message(update(owner_id, customer_id), ctx)
    await rt.commands.process_notices(ctx)
    assert hasattr(rt, "commands"), "Owner notices must survive restart"
    ctx.bot.send_message.side_effect = None
    restarted = Runtime(conf, store=ledger_db)
    await restarted.commands.process_one(ctx, now=10**12)
    await restarted.commands.process_notices(ctx, now=10**12)
    assert ctx.bot.send_message.await_args.kwargs["chat_id"] == owner_id
    assert "自动重试" in ctx.bot.send_message.await_args.kwargs["text"]


async def test_command_worker_shutdown_and_cancelled_command_restart(tmp_path, monkeypatch):
    conf = settings(tmp_path / "lifecycle.db")
    rt = Runtime(conf)
    application = build_application(conf, runtime=rt)
    fake = SimpleNamespace(bot_data={}, bot=SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=1, can_connect_to_business=True)),
        set_my_commands=AsyncMock(), send_message=AsyncMock()))
    await application.post_init(fake)
    try:
        assert "command_worker_task" in fake.bot_data
        await rt.store.upsert_business_connection("business-1", 1, True)
        entered = asyncio.Event()
        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        original = rt.store._write_stats
        monkeypatch.setattr(rt.store, "_write_stats", blocked)
        submission = asyncio.create_task(rt.business.on_business_message(update(1, 2), fake))
        await entered.wait()
        submission.cancel()
        with pytest.raises(asyncio.CancelledError):
            await submission
        monkeypatch.setattr(rt.store, "_write_stats", original)
    finally:
        await application.post_shutdown(fake)
    assert fake.bot_data["command_worker_task"].done()
    assert fake.bot_data["command_notice_worker_task"].done()
    assert rt.store.conn is None
    restarted = Runtime(conf)
    await restarted.store.open()
    try:
        assert await restarted.commands.process_one(fake, now=10**12)
        assert await restarted.store.get_balance(1, 2) == 150
    finally:
        await restarted.store.close()


async def test_queue_is_separate_and_never_initializes_financial_database(tmp_path):
    conf = settings(tmp_path / "ledger.db")
    rt = Runtime(conf)
    assert hasattr(rt, "commands"), "Recovery must have its own sidecar"
    assert rt.commands.queue.path != conf.db_path
    with sqlite3.connect(rt.commands.queue.path) as connection:
        connection.execute("CREATE TABLE ledger(secret TEXT)")
    before = rt.commands.queue.path.read_bytes()
    with pytest.raises(ValueError, match="Unexpected"):
        await rt.commands.queue.open()
    assert rt.commands.queue.path.read_bytes() == before
    assert not conf.db_path.exists()


async def test_queue_cannot_replay_against_another_ledger_in_same_log_directory(tmp_path):
    first = Runtime(settings(tmp_path / "first.db"))
    await first.commands.queue.open()
    second = Runtime(settings(tmp_path / "second.db"))
    with pytest.raises(ValueError, match="another ledger"):
        await second.commands.queue.open()


async def test_pending_duplicate_keeps_original_payload_and_retry_deadline(
    ledger_db, owner_id, customer_id, monkeypatch
):
    rt = Runtime(settings(ledger_db.db_path), store=ledger_db)
    ctx = context()
    original = ledger_db._write_stats
    monkeypatch.setattr(ledger_db, "_write_stats", AsyncMock(side_effect=fail_stats))
    await rt.business.on_business_message(update(owner_id, customer_id), ctx)
    before = await rt.commands.queue.get(owner_id, customer_id, 1)
    monkeypatch.setattr(ledger_db, "_write_stats", original)
    await rt.business.on_business_message(update(owner_id, customer_id, "+99"), ctx)
    assert await ledger_db.get_balance(owner_id, customer_id) == 0
    after = await rt.commands.queue.get(owner_id, customer_id, 1)
    assert after["due"] == before["due"]
    assert await rt.commands.process_one(ctx, now=10**12)
    assert await ledger_db.get_balance(owner_id, customer_id) == 150


async def test_clear_failure_replays_original_command_once(
    ledger_db, owner_id, customer_id, monkeypatch
):
    await ledger_db.apply_amount(owner_id, customer_id, 1, 150, "business-1")
    rt = Runtime(settings(ledger_db.db_path), store=ledger_db)
    ctx = context()
    original = ledger_db._insert_receipt_outbox
    monkeypatch.setattr(ledger_db, "_insert_receipt_outbox", AsyncMock(side_effect=fail_stats))
    await rt.business.on_business_message(update(owner_id, customer_id, "//", 2), ctx)
    assert await ledger_db.get_balance(owner_id, customer_id) == 150
    monkeypatch.setattr(ledger_db, "_insert_receipt_outbox", original)
    assert await rt.commands.process_one(ctx, now=10**12)
    assert await ledger_db.get_balance(owner_id, customer_id) == 0
    await rt.business.on_business_message(update(owner_id, customer_id, "//", 2), ctx)
    assert len(await rows(ledger_db, "ledger")) == 2


async def test_queue_preserves_integer_tenths_beyond_sqlite_integer_range(
    ledger_db, owner_id, customer_id
):
    rt = Runtime(settings(ledger_db.db_path), store=ledger_db)
    amount = 10**100
    await rt.business.on_business_message(update(owner_id, customer_id, f"+{amount}.5"), context())
    assert await ledger_db.get_balance(owner_id, customer_id) == amount * 10 + 5


async def test_shutdown_cancels_active_replay_and_restart_worker_recovers(tmp_path, monkeypatch):
    conf = settings(tmp_path / "worker.db")
    rt = Runtime(conf)
    await rt.store.open()
    await rt.store.upsert_business_connection("business-1", 1, True)
    await rt.commands.queue.enqueue(1, 2, 7, "business-1", "amount", 150)
    await rt.store.close()
    entered = asyncio.Event()
    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(rt.store, "_write_stats", blocked)
    fake = SimpleNamespace(bot_data={}, bot=SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=1, can_connect_to_business=True)),
        set_my_commands=AsyncMock(), send_message=AsyncMock(return_value=SimpleNamespace(message_id=999)),
        pin_chat_message=AsyncMock()))
    app = build_application(conf, runtime=rt)
    await app.post_init(fake)
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(app.post_shutdown(fake), 2)
    assert fake.bot_data["command_worker_task"].done()
    assert fake.bot_data["command_notice_worker_task"].done()
    restarted = Runtime(conf)
    again = build_application(conf, runtime=restarted)
    await again.post_init(fake)
    try:
        for _ in range(200):
            if (await restarted.commands.queue.get(1, 2, 7))["status"] == "done":
                break
            await asyncio.sleep(.01)
        assert await restarted.store.get_balance(1, 2) == 150
        assert len(await rows(restarted.store, "ledger")) == 1
    finally:
        await again.post_shutdown(fake)


async def test_blocked_owner_notice_never_blocks_another_owner_accounting(
    ledger_db, owner_id, customer_id
):
    rt = Runtime(settings(ledger_db.db_path), store=ledger_db)
    ctx = context()
    row = await rt.commands.queue.enqueue(owner_id, customer_id, 1, "business-1", "amount", 150)
    await rt.commands.queue.failed(row, 10**12, "SyntheticFailure")
    await ledger_db.upsert_business_connection("other", owner_id + 1, True)
    entered = asyncio.Event()
    async def blocked_notice(**kwargs):
        entered.set()
        await asyncio.Event().wait()
    ctx.bot.send_message.side_effect = blocked_notice
    ctx.bot_data["command_stop_event"] = asyncio.Event()
    worker = asyncio.create_task(rt.commands.worker(ctx))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        other = update(owner_id + 1, customer_id)
        other.business_message.business_connection_id = "other"
        await asyncio.wait_for(rt.business.on_business_message(other, ctx), .5)
        assert await ledger_db.get_balance(owner_id + 1, customer_id) == 150
        assert (await rt.commands.queue.get(owner_id, customer_id, 1))["status"] == "pending"
    finally:
        ctx.bot_data["command_stop_event"].set()
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
    assert ctx.bot_data["command_notice_worker_task"].done()


async def test_completion_preserves_recovered_notice_when_old_deferred_ack_arrives(
    ledger_db, owner_id, customer_id
):
    rt = Runtime(settings(ledger_db.db_path), store=ledger_db)
    row = await rt.commands.queue.enqueue(owner_id, customer_id, 1, "business-1", "amount", 150)
    await rt.commands.queue.failed(row, 0, "SyntheticFailure")
    in_flight = (await rt.commands.queue.notices(10))[0]
    await rt.commands.queue.complete(row)
    await rt.commands.queue.finish_notice(in_flight, 10, sent=True)
    pending = await rt.commands.queue.notices(10)
    assert len(pending) == 1
    assert pending[0]["notice"] == "recovered"


async def test_inflight_deferred_notice_then_recovery_keeps_recovered_followup(
    ledger_db, owner_id, customer_id
):
    rt = Runtime(settings(ledger_db.db_path), store=ledger_db)
    ctx = context()
    row = await rt.commands.queue.enqueue(owner_id, customer_id, 1, "business-1", "amount", 150)
    await rt.commands.queue.failed(row, 0, "SyntheticFailure")
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed_notice(**kwargs):
        entered.set()
        await release.wait()
    ctx.bot.send_message.side_effect = delayed_notice
    notice = asyncio.create_task(rt.commands.process_notices(ctx, now=10))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert await asyncio.wait_for(rt.commands.process_one(ctx, now=10), .5)
        assert await ledger_db.get_balance(owner_id, customer_id) == 150
    finally:
        release.set()
        await notice
    assert (await rt.commands.queue.get(owner_id, customer_id, 1))["notice"] == "recovered"
    ctx.bot.send_message.side_effect = None
    await rt.commands.process_notices(ctx, now=10)
    assert "已确认入账" in ctx.bot.send_message.await_args.kwargs["text"]
    assert (await rt.commands.queue.get(owner_id, customer_id, 1))["notice"] is None


async def test_owner_notice_timeout_retains_notice_for_bounded_retry(
    ledger_db, owner_id, customer_id, monkeypatch
):
    rt = Runtime(settings(ledger_db.db_path), store=ledger_db)
    ctx = context()
    row = await rt.commands.queue.enqueue(owner_id, customer_id, 1, "business-1", "amount", 150)
    await rt.commands.queue.failed(row, 0, "SyntheticFailure")
    monkeypatch.setattr("jzbot.services.commands.NOTICE_SEND_TIMEOUT", .01)
    async def blocked_notice(**kwargs):
        await asyncio.Event().wait()
    ctx.bot.send_message.side_effect = blocked_notice
    await asyncio.wait_for(rt.commands.process_notices(ctx, now=10), .5)
    pending = await rt.commands.queue.get(owner_id, customer_id, 1)
    assert pending["notice"] == "deferred"
    assert pending["notice_due"] == 40
    ctx.bot.send_message.side_effect = None
    await rt.commands.process_notices(ctx, now=39)
    assert ctx.bot.send_message.await_count == 1
    await rt.commands.process_notices(ctx, now=40)
    assert ctx.bot.send_message.await_count == 2
    assert (await rt.commands.queue.get(owner_id, customer_id, 1))["notice"] is None
