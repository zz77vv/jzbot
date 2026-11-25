"""Tests for OutboxWorker and BroadcastWorker with mocked Bot."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import RetryAfter, TimedOut

from jzbot.broadcast import BroadcastWorker
from jzbot.outbox import OutboxWorker
from jzbot.store import Store


@pytest.mark.asyncio
async def test_outbox_deliver_and_pin(tmp_path: Path):
    store = Store(tmp_path / "jzbot.db")
    try:
        store.upsert_connection("c1", 1, True)
        store.record_amount(owner_id=1, chat_id=10, source_message_id=5, connection_id="c1", kind="deposit", amount_tenths=100)

        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=99)),
            pin_chat_message=AsyncMock(),
        )
        worker = OutboxWorker(store, bot)
        delivered = await worker.process_due_once()
        assert delivered == 1
        bot.send_message.assert_awaited_once()
        bot.pin_chat_message.assert_awaited_once_with(
            chat_id=10,
            message_id=99,
            business_connection_id="c1",
            disable_notification=True,
        )
        assert store.get_due_receipts() == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_outbox_retry_after_keeps_pending(tmp_path: Path):
    store = Store(tmp_path / "jzbot.db")
    try:
        store.upsert_connection("c1", 1, True)
        store.record_amount(owner_id=1, chat_id=10, source_message_id=5, connection_id="c1", kind="deposit", amount_tenths=100)

        bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=RetryAfter(12)),
            pin_chat_message=AsyncMock(),
        )
        worker = OutboxWorker(store, bot)
        delivered = await worker.process_due_once()
        assert delivered == 0
        pending, total = store.list_pending_receipts(1)
        assert total == 1
        assert pending[0].attempt_count == 1
        assert "RetryAfter" in (pending[0].last_error or "")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_broadcast_worker_sent_and_uncertain(tmp_path: Path):
    store = Store(tmp_path / "jzbot.db")
    try:
        store.upsert_connection("c1", 1, True)
        job_id = store.create_broadcast_job(1, ["traded"], "Notice!", [10, 11])

        conn_info = SimpleNamespace(user=SimpleNamespace(id=1), is_enabled=True, rights=SimpleNamespace(can_reply=True))
        bot = SimpleNamespace(
            get_business_connection=AsyncMock(return_value=conn_info),
            send_message=AsyncMock(side_effect=[SimpleNamespace(message_id=501), TimedOut()]),
        )
        worker = BroadcastWorker(store, bot)
        assert await worker.process_one() is True
        assert await worker.process_one() is True
        # Third call sees no pending items and auto-completes job
        assert await worker.process_one() is False

        counts = store.get_broadcast_counts(job_id)
        assert counts["sent"] == 1
        assert counts["uncertain"] == 1
        assert store.get_active_broadcast(1) is None
    finally:
        store.close()
