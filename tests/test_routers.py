"""Smoke test verifying aiogram 3 Dispatcher + Business/Admin router wiring."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update

from ledgerflow.handlers.admin import create_admin_router
from ledgerflow.handlers.business import create_business_router, render_statement_card
from ledgerflow.repository import LedgerRepository
from ledgerflow.ui import render_contact_detail_view, render_home_view, render_settings_view, strip_button_icons
from ledgerflow.workers import AnnouncementDispatcher, ReceiptDispatcher


def test_routers_wire_into_dispatcher_and_render_statement(tmp_path: Path):
    repo = LedgerRepository(tmp_path / "lf.db")
    try:
        bot = SimpleNamespace()
        receipts = ReceiptDispatcher(repo, bot)
        announcer = AnnouncementDispatcher(repo, bot)
        dp = Dispatcher()
        dp.include_router(create_business_router(repo, receipts))
        dp.include_router(create_admin_router(repo, receipts, announcer, tmp_path / "bak"))
        assert len(dp.sub_routers) == 2

        repo.upsert_link("L1", 1, True)
        repo.post_entry(owner_id=1, chat_id=20, tg_msg_id=1, link_id="L1", kind="in", amount_tenths=500, note="测试单")
        card = render_statement_card(repo, 1, 20)
        assert "50 USDT" in card
        assert "测试单" in card
        assert "<blockquote expandable>" in card
        assert '<tg-emoji emoji-id="' in card
        assert "#1" not in card

        home_txt, home_kb = render_home_view(repo.get_dashboard(1))
        assert "记账助手" in home_txt
        assert "客户预存" in home_txt
        assert "客户未结" in home_txt
        assert "账面净额" in home_txt
        assert '<tg-emoji emoji-id="' in home_txt
        assert home_kb.inline_keyboard[0][0].style == "success"
        assert home_kb.inline_keyboard[0][0].icon_custom_emoji_id == "5416081784641168838"
        assert home_kb.inline_keyboard[0][1].style == "danger"
        assert home_kb.inline_keyboard[0][2].style == "primary"
        assert all("导出" not in b.text and "搜索" not in b.text for row in home_kb.inline_keyboard for b in row)

        stripped = strip_button_icons(home_kb)
        assert all(b.icon_custom_emoji_id is None for row in stripped.inline_keyboard for b in row)

        c = repo.get_contact(1, 20)
        txs, _ = repo.get_journal(1, 20, limit=3)
        dt_txt, dt_kb = render_contact_detail_view(c, txs, "lst:all:0")
        assert "<blockquote expandable>" in dt_txt
        assert "#1" in dt_txt
        assert all("撤销" not in b.text and "导出" not in b.text and "归档" not in b.text for row in dt_kb.inline_keyboard for b in row)
        assert any(b.copy_text and b.copy_text.text == "20" for row in dt_kb.inline_keyboard for b in row)

        _, cfg_kb = render_settings_view(repo.get_dashboard(1))
        assert all("归档" not in b.text for row in cfg_kb.inline_keyboard for b in row)
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_admin_text_input_prompts(tmp_path: Path):
    repo = LedgerRepository(tmp_path / "lf.db")
    bot = Bot(token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    bot.session.make_request = AsyncMock(return_value=True)
    try:
        repo.upsert_link("L1", 1, True)
        repo.post_entry(owner_id=1, chat_id=20, tg_msg_id=1, link_id="L1", kind="in", amount_tenths=500)

        receipts = ReceiptDispatcher(repo, bot)
        announcer = AnnouncementDispatcher(repo, bot)
        dp = Dispatcher()
        dp.include_router(create_admin_router(repo, receipts, announcer, tmp_path / "bak"))

        def make_msg_update(uid: int, text: str, mid: int) -> Update:
            return Update.model_validate(
                {
                    "update_id": uid,
                    "message": {
                        "message_id": mid,
                        "date": 1700000000,
                        "chat": {"id": 1, "type": "private"},
                        "from": {"id": 1, "is_bot": False, "first_name": "Owner"},
                        "text": text,
                    },
                }
            )

        def make_cb_update(uid: int, data: str, mid: int) -> Update:
            return Update.model_validate(
                {
                    "update_id": uid,
                    "callback_query": {
                        "id": f"cb_{uid}",
                        "from": {"id": 1, "is_bot": False, "first_name": "Owner"},
                        "chat_instance": "ci",
                        "data": data,
                        "message": {
                            "message_id": mid,
                            "date": 1700000000,
                            "chat": {"id": 1, "type": "private"},
                            "text": "card",
                        },
                    },
                }
            )

        # Idle private text without prompt should be ignored cleanly (no NameError)
        await dp.feed_update(bot, make_msg_update(1, "你好", 10))

        # Trigger alias prompt -> set alias
        await dp.feed_update(bot, make_cb_update(2, "alias:20", 11))
        await dp.feed_update(bot, make_msg_update(3, "老客户A", 12))
        assert repo.get_contact(1, 20).alias == "老客户A"

        # Trigger tags prompt -> set tags
        await dp.feed_update(bot, make_cb_update(4, "tagset:20", 13))
        await dp.feed_update(bot, make_msg_update(5, "VIP 重点", 14))
        assert repo.get_contact(1, 20).tags == ["VIP", "重点"]

        # Trigger adjust prompt -> invalid text ignored, valid adjustment applied
        await dp.feed_update(bot, make_cb_update(6, "adj:20", 15))
        await dp.feed_update(bot, make_msg_update(7, "not_a_number", 16))
        assert repo.get_contact(1, 20).balance_tenths == 500
        await dp.feed_update(bot, make_msg_update(8, "+25.5 补差", 17))
        assert repo.get_contact(1, 20).balance_tenths == 755

        # Overflow guard in admin_adjust
        with pytest.raises(OverflowError):
            repo.admin_adjust(owner_id=1, chat_id=20, delta_tenths=9_000_000_000_000_000_000, note="overflow")
    finally:
        await bot.session.close()
        repo.close()
