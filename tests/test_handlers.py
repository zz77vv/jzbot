"""Tests for BusinessHandlers and AdminHandlers with mocked Telegram updates."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ChatType

from jzbot.admin import AdminHandlers
from jzbot.broadcast import BroadcastWorker
from jzbot.business import BusinessHandlers
from jzbot.outbox import OutboxWorker
from jzbot.store import Store


@pytest.mark.asyncio
async def test_business_handlers_deposit_query_and_customer_ignored(tmp_path: Path):
    store = Store(tmp_path / "jzbot.db")
    try:
        bot = SimpleNamespace(send_message=AsyncMock(), pin_chat_message=AsyncMock())
        outbox = OutboxWorker(store, bot)
        biz = BusinessHandlers(store, outbox)

        # Register connection -> triggers one-time welcome to owner
        conn_upd = SimpleNamespace(business_connection=SimpleNamespace(id="c1", user=SimpleNamespace(id=100), is_enabled=True))
        ctx = SimpleNamespace(bot=bot)
        await biz.on_business_connection(conn_upd, ctx)
        assert bot.send_message.await_count == 1
        bot.send_message.reset_mock()
        # Re-receiving connection does NOT re-send welcome
        await biz.on_business_connection(conn_upd, ctx)
        assert bot.send_message.await_count == 0

        # Customer sending +50 must be ignored
        cust_msg = SimpleNamespace(
            chat=SimpleNamespace(id=200, type=ChatType.PRIVATE, username="alice", first_name="Alice", last_name=None),
            from_user=SimpleNamespace(id=200, is_bot=False),
            business_connection_id="c1",
            message_id=1,
            text="+50",
        )
        await biz.on_business_message(SimpleNamespace(business_message=cust_msg), ctx)
        assert store.get_customer(100, 200).has_wallet is False

        # Owner sending +25.5 must be recorded
        owner_msg = SimpleNamespace(
            chat=SimpleNamespace(id=200, type=ChatType.PRIVATE, username="alice", first_name="Alice", last_name=None),
            from_user=SimpleNamespace(id=100, is_bot=False),
            business_connection_id="c1",
            message_id=2,
            text="+25.5",
        )
        await biz.on_business_message(SimpleNamespace(business_message=owner_msg), ctx)
        assert store.get_customer(100, 200).balance_tenths == 255

        # Owner sending `/` replies with recent transactions & balance
        query_msg = SimpleNamespace(
            chat=SimpleNamespace(id=200, type=ChatType.PRIVATE, username="alice", first_name="Alice", last_name=None),
            from_user=SimpleNamespace(id=100, is_bot=False),
            business_connection_id="c1",
            message_id=3,
            text="/",
        )
        await biz.on_business_message(SimpleNamespace(business_message=query_msg), ctx)
        bot.send_message.assert_awaited_once()
        sent_text = bot.send_message.call_args.kwargs["text"]
        assert "25.5 USDT" in sent_text
        assert "+25.5 USDT" in sent_text
    finally:
        store.close()


@pytest.mark.asyncio
async def test_admin_start_and_zero_flow(tmp_path: Path):
    store = Store(tmp_path / "jzbot.db")
    try:
        store.upsert_connection("c1", 100, True)
        store.record_amount(owner_id=100, chat_id=200, source_message_id=1, connection_id="c1", kind="deposit", amount_tenths=300, first_name="Alice")

        bot = SimpleNamespace(send_message=AsyncMock())
        adm = AdminHandlers(store, OutboxWorker(store, bot), BroadcastWorker(store, bot))

        reply_mock = AsyncMock()
        start_upd = SimpleNamespace(
            effective_chat=SimpleNamespace(id=100, type=ChatType.PRIVATE),
            effective_user=SimpleNamespace(id=100),
            message=SimpleNamespace(reply_text=reply_mock),
        )
        await adm.on_start(start_upd, SimpleNamespace(bot=bot))
        reply_mock.assert_awaited_once()
        assert "记账管理后台" in reply_mock.call_args.args[0]

        # Ask to zero customer 200
        edit_mock = AsyncMock()
        answer_mock = AsyncMock()
        cb_ask = SimpleNamespace(
            effective_chat=SimpleNamespace(id=100, type=ChatType.PRIVATE),
            effective_user=SimpleNamespace(id=100),
            callback_query=SimpleNamespace(data="zero_ask:200", answer=answer_mock, edit_message_text=edit_mock),
        )
        await adm.on_callback(cb_ask, SimpleNamespace(bot=bot))
        kb = edit_mock.call_args.kwargs["reply_markup"]
        confirm_cb = kb.inline_keyboard[0][0].callback_data
        assert confirm_cb.startswith("zero_do:")

        # Confirm zero
        cb_do = SimpleNamespace(
            effective_chat=SimpleNamespace(id=100, type=ChatType.PRIVATE),
            effective_user=SimpleNamespace(id=100),
            callback_query=SimpleNamespace(data=confirm_cb, answer=answer_mock, edit_message_text=edit_mock),
        )
        await adm.on_callback(cb_do, SimpleNamespace(bot=bot))
        assert store.get_customer(100, 200).balance_tenths == 0
    finally:
        store.close()
