"""Tests for aiogram 3 ReceiptDispatcher and AnnouncementDispatcher."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage

from ledgerflow.repository import LedgerRepository
from ledgerflow.workers import AnnouncementDispatcher, ReceiptDispatcher


@pytest.mark.asyncio
async def test_receipt_dispatcher_deliver_and_pin(tmp_path: Path):
    repo = LedgerRepository(tmp_path / "lf.db")
    try:
        repo.upsert_link("L1", 1, True)
        repo.post_entry(owner_id=1, chat_id=10, tg_msg_id=1, link_id="L1", kind="in", amount_tenths=100)

        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=77)),
            pin_chat_message=AsyncMock(),
        )
        disp = ReceiptDispatcher(repo, bot)
        assert await disp.flush_due_once() == 1
        bot.send_message.assert_awaited_once()
        bot.pin_chat_message.assert_awaited_once()
        assert repo.get_due_receipts() == []
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_receipt_retry_after(tmp_path: Path):
    repo = LedgerRepository(tmp_path / "lf.db")
    try:
        repo.upsert_link("L1", 1, True)
        repo.post_entry(owner_id=1, chat_id=10, tg_msg_id=1, link_id="L1", kind="in", amount_tenths=100)
        err = TelegramRetryAfter(method=SendMessage(chat_id=10, text="x"), message="flood", retry_after=15)
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=err), pin_chat_message=AsyncMock())
        disp = ReceiptDispatcher(repo, bot)
        assert await disp.flush_due_once() == 0
        pending, cnt = repo.list_pending_receipts(1)
        assert cnt == 1 and pending[0].attempts == 1
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_announcement_dispatcher_sent_and_uncertain(tmp_path: Path):
    repo = LedgerRepository(tmp_path / "lf.db")
    try:
        repo.upsert_link("L1", 1, True)
        ann_id = repo.create_announcement(1, "tag:VIP", "Important update", [10, 11])
        link_info = SimpleNamespace(user=SimpleNamespace(id=1), is_enabled=True, rights=SimpleNamespace(can_reply=True))
        net_err = TelegramNetworkError(method=SendMessage(chat_id=11, text="x"), message="timeout")
        bot = SimpleNamespace(
            get_business_connection=AsyncMock(return_value=link_info),
            send_message=AsyncMock(side_effect=[SimpleNamespace(message_id=901), net_err]),
        )
        disp = AnnouncementDispatcher(repo, bot)
        assert await disp.step_once() is True
        assert await disp.step_once() is True
        assert await disp.step_once() is False
        counts = repo.get_announcement_counts(ann_id)
        assert counts["sent"] == 1 and counts["uncertain"] == 1
    finally:
        repo.close()
