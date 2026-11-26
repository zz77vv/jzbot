"""LedgerFlow aiogram 3 application lifecycle, logging, and polling entry point."""

from __future__ import annotations

import asyncio
import logging
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from ledgerflow.config import AppConfig, ProcessLock
from ledgerflow.handlers.admin import create_admin_router
from ledgerflow.handlers.business import create_business_router
from ledgerflow.repository import LedgerRepository
from ledgerflow.workers import AnnouncementDispatcher, ReceiptDispatcher

logger = logging.getLogger(__name__)


def setup_logging(cfg: AppConfig) -> None:
    cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    fh = RotatingFileHandler(cfg.log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def run_async(cfg: AppConfig) -> None:
    repo = LedgerRepository(cfg.db_path, cfg.currency_unit)
    ok, fk = repo.health_check()
    if not ok or fk != 0:
        repo.close()
        raise RuntimeError(f"Database health check failed (quick_check={ok}, fk_errors={fk})")

    bot = Bot(token=cfg.bot_token)
    dp = Dispatcher()

    receipts = ReceiptDispatcher(repo, bot)
    announcer = AnnouncementDispatcher(repo, bot)

    dp.include_router(create_business_router(repo, receipts, cfg.tz_offset_hours))
    dp.include_router(create_admin_router(repo, receipts, announcer, cfg.backup_dir, cfg.tz_offset_hours))

    @dp.startup()
    async def on_startup() -> None:
        me = await bot.get_me()
        logger.info("LedgerFlow authenticated as @%s (id=%s, business=%s)", me.username, me.id, getattr(me, "can_connect_to_business", None))
        await bot.set_my_commands([BotCommand(command="start", description="打开记账财务总控台")])
        receipts.start()
        announcer.start()

    @dp.shutdown()
    async def on_shutdown() -> None:
        await announcer.stop()
        await receipts.stop()
        repo.close()
        await bot.session.close()
        logger.info("LedgerFlow shutdown complete")

    logger.info("Starting LedgerFlow polling (db=%s)", cfg.db_path)
    await dp.start_polling(
        bot,
        allowed_updates=["business_connection", "business_message", "message", "callback_query"],
    )


def main() -> None:
    cfg = AppConfig.load()
    setup_logging(cfg)
    with ProcessLock(cfg.lock_path):
        asyncio.run(run_async(cfg))
