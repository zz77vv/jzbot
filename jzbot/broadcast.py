"""Background broadcast worker with rate limiting and safe restart recovery."""

from __future__ import annotations

import asyncio
import logging

from telegram import Bot
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut

from jzbot.money import retry_after_seconds
from jzbot.store import Store

logger = logging.getLogger(__name__)

BROADCAST_INTERVAL_SECONDS = 1.2
MAX_RETRY_AFTER_ATTEMPTS = 3


class BroadcastWorker:
    """Sends broadcast messages one at a time, yielding when receipts are due."""

    def __init__(self, store: Store, bot: Bot) -> None:
        self.store = store
        self.bot = bot
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def notify(self) -> None:
        self._wake.set()

    def start(self) -> None:
        self.store.recover_broadcasts_on_startup()
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="jzbot-broadcast-worker")

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
                if self.store.has_due_receipts():
                    await asyncio.sleep(0.5)
                    continue
                worked = await self.process_one()
                if worked:
                    await asyncio.sleep(BROADCAST_INTERVAL_SECONDS)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unexpected error in broadcast worker")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass

    async def _resolve_connection(self, owner_id: int) -> str | None:
        candidates = self.store.list_owner_connections(owner_id, limit=8)
        for conn_id, is_enabled in candidates:
            if not is_enabled:
                continue
            try:
                info = await self.bot.get_business_connection(conn_id)
                if info.user.id != owner_id or not info.is_enabled:
                    self.store.upsert_connection(conn_id, info.user.id, bool(info.is_enabled))
                    continue
                rights = getattr(info, "rights", None)
                if rights is not None and getattr(rights, "can_reply", True) is False:
                    continue
                return conn_id
            except (BadRequest, Forbidden):
                self.store.upsert_connection(conn_id, owner_id, False)
            except (TimedOut, NetworkError):
                # Network error must not disable stored connection
                return conn_id
            except TelegramError:
                continue
        return None

    async def process_one(self) -> bool:
        claimed = self.store.claim_next_broadcast_recipient()
        if claimed is None:
            return False
        job, rec = claimed
        conn_id = await self._resolve_connection(job.owner_id)
        if not conn_id:
            self.store.finish_broadcast_recipient(rec.id, "failed", error_reason="No active business connection")
            return True

        try:
            sent = await self.bot.send_message(
                chat_id=rec.chat_id,
                text=job.message_text,
                business_connection_id=conn_id,
                disable_notification=True,
            )
            self.store.finish_broadcast_recipient(rec.id, "sent", message_id=sent.message_id)
        except RetryAfter as exc:
            delay = retry_after_seconds(exc.retry_after)
            if rec.attempts < MAX_RETRY_AFTER_ATTEMPTS:
                self.store.finish_broadcast_recipient(
                    rec.id,
                    "pending",
                    error_reason=f"RetryAfter {delay}s",
                    retry_after_seconds=delay,
                )
            else:
                self.store.finish_broadcast_recipient(rec.id, "failed", error_reason=f"RetryAfter exhausted: {exc}")
        except (TimedOut, NetworkError) as exc:
            # Ambiguous delivery: never auto-resend
            self.store.finish_broadcast_recipient(rec.id, "uncertain", error_reason=f"Uncertain network result: {exc}")
        except (BadRequest, Forbidden, TelegramError) as exc:
            self.store.finish_broadcast_recipient(rec.id, "failed", error_reason=f"{type(exc).__name__}: {exc}")
        return True
