"""Smoke test verifying aiogram 3 Dispatcher + Business/Admin router wiring."""

from pathlib import Path
from types import SimpleNamespace

from aiogram import Dispatcher

from ledgerflow.handlers.admin import create_admin_router
from ledgerflow.handlers.business import create_business_router, render_statement_card
from ledgerflow.repository import LedgerRepository
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
    finally:
        repo.close()
