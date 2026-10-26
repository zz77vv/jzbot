"""The only production assembly/polling entry point."""

import asyncio
import contextlib
import logging

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    BusinessConnectionHandler,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from . import __version__
from .config import Settings
from .infra.instance_lock import SingleInstanceLock
from .infra.logging import configure_logging
from .runtime import Runtime

logger = logging.getLogger("jzbot")


def build_application(settings, *, runtime=None):
    runtime = runtime if runtime is not None else Runtime(settings)

    async def initialize(application):
        try:
            await runtime.store.open()
            me = await application.bot.get_me()
            if not me.can_connect_to_business:
                raise RuntimeError(
                    "Production bot does not support Business connections"
                )
            await application.bot.set_my_commands([BotCommand("start", "打开账本管理")])
            stop = asyncio.Event()
            application.bot_data["outbox_stop_event"] = stop
            application.bot_data["outbox_worker_task"] = asyncio.create_task(
                runtime.outbox.outbox_worker(application), name="receipt-outbox-worker"
            )
            logger.info(
                "V2 started | version=%s release=%s bot_id=%s business_capable=%s outbox_worker=started",
                __version__,
                settings.release,
                me.id,
                me.can_connect_to_business,
            )
        except BaseException:
            await runtime.store.close()
            raise

    async def shutdown(application):
        stop = application.bot_data.get("outbox_stop_event")
        if stop is not None:
            stop.set()
        worker = application.bot_data.get("outbox_worker_task")
        try:
            if worker is not None:
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker
        finally:
            await runtime.store.close()
            logger.info("V2 stopped")

    application = (
        Application.builder()
        .token(settings.token)
        .concurrent_updates(1)
        .post_init(initialize)
        .post_shutdown(shutdown)
        .build()
    )
    application.bot_data["runtime"] = runtime
    application.add_handler(CommandHandler("start", runtime.admin.on_start))
    application.add_handler(CommandHandler("cancel", runtime.admin.on_cancel_search))
    application.add_handler(CallbackQueryHandler(runtime.admin.on_admin_callback))
    application.add_handler(
        MessageHandler(
            filters.UpdateType.MESSAGE
            & filters.ChatType.PRIVATE
            & filters.TEXT
            & ~filters.COMMAND,
            runtime.admin.on_admin_search_text,
        )
    )
    application.add_handler(
        BusinessConnectionHandler(runtime.business.on_business_connection)
    )
    application.add_handler(
        MessageHandler(
            filters.UpdateType.BUSINESS_MESSAGE & filters.TEXT,
            runtime.business.on_business_message,
        )
    )
    return application


def main():
    settings = Settings.from_environment()
    # Shared with V1 when using the same ledger; never delete this file.
    with SingleInstanceLock(settings.lock_path):
        configure_logging(settings.log_path, settings.token)
        application = build_application(settings)
        application.run_polling(
            allowed_updates=Update.ALL_TYPES, drop_pending_updates=False
        )
