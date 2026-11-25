"""Application wiring, logging setup, and lifecycle management."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    BusinessConnectionHandler,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from jzbot.admin import AdminHandlers
from jzbot.broadcast import BroadcastWorker
from jzbot.business import BusinessHandlers
from jzbot.config import InstanceLock, Settings
from jzbot.outbox import OutboxWorker
from jzbot.store import Store

logger = logging.getLogger(__name__)


def configure_logging(settings: Settings) -> None:
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    fh = RotatingFileHandler(settings.log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def build_application(settings: Settings) -> tuple[Application, Store, OutboxWorker, BroadcastWorker]:
    store = Store(settings.db_path)
    ok, fk_errors = store.integrity_check()
    if not ok or fk_errors:
        store.close()
        raise RuntimeError(f"Database integrity check failed (quick_check={ok}, fk_errors={fk_errors})")

    outbox_holder: list[OutboxWorker] = []
    broadcast_holder: list[BroadcastWorker] = []

    async def post_init(app: Application) -> None:
        me = await app.bot.get_me()
        logger.info("Bot authenticated as @%s (id=%s, can_connect_to_business=%s)", me.username, me.id, getattr(me, "can_connect_to_business", None))
        await app.bot.set_my_commands([BotCommand("start", "打开记账管理后台")])
        outbox_holder[0].start()
        broadcast_holder[0].start()

    async def post_shutdown(app: Application) -> None:
        if broadcast_holder:
            await broadcast_holder[0].stop()
        if outbox_holder:
            await outbox_holder[0].stop()
        store.close()
        logger.info("JZBot shutdown complete")

    app = (
        ApplicationBuilder()
        .token(settings.token)
        .concurrent_updates(1)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    outbox = OutboxWorker(store, app.bot)
    broadcaster = BroadcastWorker(store, app.bot)
    outbox_holder.append(outbox)
    broadcast_holder.append(broadcaster)

    biz = BusinessHandlers(store, outbox, settings.timezone_offset_hours)
    adm = AdminHandlers(
        store,
        outbox,
        broadcaster,
        tz_offset_hours=settings.timezone_offset_hours,
        backup_dir=settings.backup_dir,
    )

    app.add_handler(BusinessConnectionHandler(biz.on_business_connection))
    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE & filters.TEXT, biz.on_business_message))
    app.add_handler(CommandHandler("start", adm.on_start, filters=filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(adm.on_callback))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, adm.on_private_text))

    return (app, store, outbox, broadcaster)


def run() -> None:
    settings = Settings.load()
    configure_logging(settings)
    with InstanceLock(settings.lock_path):
        app, _, _, _ = build_application(settings)
        logger.info("Starting JZBot polling (db=%s)", settings.db_path)
        app.run_polling(
            allowed_updates=[
                Update.BUSINESS_CONNECTION,
                Update.BUSINESS_MESSAGE,
                Update.MESSAGE,
                Update.CALLBACK_QUERY,
            ],
            drop_pending_updates=False,
        )
