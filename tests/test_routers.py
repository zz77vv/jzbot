"""Smoke test verifying aiogram 3 Dispatcher + Business/Admin router wiring."""

from pathlib import Path
from types import SimpleNamespace

from aiogram import Dispatcher

from ledgerflow.handlers.admin import create_admin_router
from ledgerflow.handlers.business import create_business_router, render_statement_card
from ledgerflow.repository import LedgerRepository
from ledgerflow.ui import render_contact_detail_view, render_home_view, strip_button_icons
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
        assert '<tg-emoji emoji-id="' in home_txt
        assert home_kb.inline_keyboard[0][0].style == "success"
        assert home_kb.inline_keyboard[0][0].icon_custom_emoji_id == "5416081784641168838"
        assert home_kb.inline_keyboard[0][1].style == "danger"
        assert home_kb.inline_keyboard[0][2].style == "primary"
        assert all("导出" not in b.text for row in home_kb.inline_keyboard for b in row)

        stripped = strip_button_icons(home_kb)
        assert all(b.icon_custom_emoji_id is None for row in stripped.inline_keyboard for b in row)

        c = repo.get_contact(1, 20)
        txs, _ = repo.get_journal(1, 20, limit=3)
        dt_txt, dt_kb = render_contact_detail_view(c, txs, "lst:all:0")
        assert "<blockquote expandable>" in dt_txt
        assert "#1" in dt_txt
        assert all("撤销" not in b.text and "导出" not in b.text for row in dt_kb.inline_keyboard for b in row)
        assert any(b.copy_text and b.copy_text.text == "20" for row in dt_kb.inline_keyboard for b in row)
    finally:
        repo.close()
