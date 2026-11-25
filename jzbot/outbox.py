"""Background receipt outbox delivery and pinning worker."""

from __future__ import annotations

import asyncio
import logging

from telegram import Bot
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from jzbot.money import retry_after_seconds
from jzbot.store import ReceiptRow, Store

logger = logging.getLogger(__name__)

BACKOFF_SECONDS = (10, 30, 120, 300)


class OutboxWorker:
    """Delivers pending receipts from SQLite outbox with backoff and optional pinning."""

    def __init__(self, store: Store, bot: Bot) -> None:
        self.store = store
        self.bot = bot
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._delivery_lock = asyncio.Lock()

    def notify(self) -> None:
        self._wake.set()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="jzbot-outbox-worker")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.process_due_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unexpected error in outbox worker loop")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

    async def process_due_once(self) -> int:
        delivered = 0
        async with self._delivery_lock:
            due = self.store.get_due_receipts(limit=20)
            for receipt in due:
                if self._stop.is_set():
                    break
                ok = await self._deliver_one(receipt)
                if ok:
                    delivered += 1
        return delivered

    async def _deliver_one(self, receipt: ReceiptRow) -> bool:
        conn_id = receipt.connection_id
        try:
            sent = await self.bot.send_message(
                chat_id=receipt.chat_id,
                text=receipt.receipt_text,
                business_connection_id=conn_id,
                reply_to_message_id=receipt.reply_to_message_id,
                allow_sending_without_reply=True,
            )
        except RetryAfter as exc:
            delay = retry_after_seconds(exc.retry_after)
            self.store.mark_receipt_retry(receipt.id, delay, f"RetryAfter: {delay}s")
            logger.warning("Receipt %s hit RetryAfter (%ss)", receipt.id, delay)
            return False
        except BadRequest as exc:
            # If the owner reconnected Business Mode with a new connection_id, switch to the newest enabled one.
            for alt_id, enabled in self.store.list_owner_connections(receipt.owner_id, limit=4):
                if enabled and alt_id != conn_id:
                    self.store.update_receipt_connection(receipt.id, alt_id)
                    logger.info("Receipt %s switched connection to newer %s after BadRequest", receipt.id, alt_id)
                    break
            idx = min(receipt.attempt_count, len(BACKOFF_SECONDS) - 1)
            delay = float(BACKOFF_SECONDS[idx])
            self.store.mark_receipt_retry(receipt.id, delay, f"BadRequest: {exc}")
            logger.warning("Receipt %s send failed (attempt %s): %s", receipt.id, receipt.attempt_count + 1, exc)
            return False
        except (Forbidden, TelegramError) as exc:
            idx = min(receipt.attempt_count, len(BACKOFF_SECONDS) - 1)
            delay = float(BACKOFF_SECONDS[idx])
            self.store.mark_receipt_retry(receipt.id, delay, f"{type(exc).__name__}: {exc}")
            logger.warning("Receipt %s send failed (attempt %s): %s", receipt.id, receipt.attempt_count + 1, exc)
            return False
        except Exception as exc:
            idx = min(receipt.attempt_count, len(BACKOFF_SECONDS) - 1)
            delay = float(BACKOFF_SECONDS[idx])
            self.store.mark_receipt_retry(receipt.id, delay, f"{type(exc).__name__}: {exc}")
            logger.exception("Receipt %s unexpected send failure", receipt.id)
            return False

        # Receipt is confirmed delivered to Telegram; mark delivered before best-effort pin.
        self.store.mark_receipt_delivered(receipt.id)

        if receipt.should_pin and getattr(sent, "message_id", None):
            try:
                await self.bot.pin_chat_message(
                    chat_id=receipt.chat_id,
                    message_id=sent.message_id,
                    business_connection_id=conn_id,
                    disable_notification=True,
                )
            except TelegramError as exc:
                logger.info("Best-effort pin failed for receipt %s: %s", receipt.id, exc)
        return True
