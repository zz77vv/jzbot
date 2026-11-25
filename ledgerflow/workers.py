"""Async background dispatchers for guaranteed receipts and rate-limited announcements (aiogram 3)."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import ReplyParameters

from ledgerflow.repository import LedgerRepository, ReceiptItem

logger = logging.getLogger(__name__)

RECEIPT_BACKOFF = (10.0, 30.0, 120.0, 300.0)
ANNOUNCE_PACE_SECONDS = 1.2
MAX_RETRY_AFTER = 3


class ReceiptDispatcher:
    """Delivers queued HTML receipts and optional pins with automatic retry & connection failover."""

    def __init__(self, repo: LedgerRepository, bot: Bot) -> None:
        self.repo = repo
        self.bot = bot
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    def wake(self) -> None:
        self._wake.set()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="ledgerflow-receipts")

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

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.flush_due_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ReceiptDispatcher loop error")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

    async def flush_due_once(self) -> int:
        sent_count = 0
        async with self._lock:
            for item in self.repo.get_due_receipts(limit=20):
                if self._stop.is_set():
                    break
                if await self._send_one(item):
                    sent_count += 1
        return sent_count

    async def _send_one(self, item: ReceiptItem) -> bool:
        link_id = item.link_id
        reply_params = ReplyParameters(message_id=item.reply_msg_id, allow_sending_without_reply=True) if item.reply_msg_id else None
        try:
            sent = await self.bot.send_message(
                chat_id=item.chat_id,
                text=item.html_text,
                parse_mode=ParseMode.HTML,
                business_connection_id=link_id,
                reply_parameters=reply_params,
            )
        except TelegramRetryAfter as exc:
            delay = max(float(exc.retry_after), 1.0)
            self.repo.mark_receipt_retry(item.id, delay, f"RetryAfter {delay}s")
            logger.warning("Receipt #%s rate-limited (%ss)", item.id, delay)
            return False
        except TelegramBadRequest as exc:
            # Auto-switch to owner's newest active business connection if old link expired
            for alt_id, enabled in self.repo.list_owner_links(item.owner_id, limit=4):
                if enabled and alt_id != link_id:
                    self.repo.switch_receipt_link(item.id, alt_id)
                    logger.info("Receipt #%s switched to newer business link %s", item.id, alt_id)
                    break
            delay = RECEIPT_BACKOFF[min(item.attempts, len(RECEIPT_BACKOFF) - 1)]
            self.repo.mark_receipt_retry(item.id, delay, f"BadRequest: {exc}")
            return False
        except (TelegramForbiddenError, TelegramAPIError, Exception) as exc:
            delay = RECEIPT_BACKOFF[min(item.attempts, len(RECEIPT_BACKOFF) - 1)]
            self.repo.mark_receipt_retry(item.id, delay, f"{type(exc).__name__}: {exc}")
            logger.warning("Receipt #%s delivery failed (attempt %s): %s", item.id, item.attempts + 1, exc)
            return False

        self.repo.mark_receipt_sent(item.id)
        if item.should_pin and getattr(sent, "message_id", None):
            try:
                await self.bot.pin_chat_message(
                    chat_id=item.chat_id,
                    message_id=sent.message_id,
                    business_connection_id=link_id,
                    disable_notification=True,
                )
            except TelegramAPIError as exc:
                logger.info("Best-effort pin skipped for receipt #%s: %s", item.id, exc)
        return True


class AnnouncementDispatcher:
    """Rate-limited targeted notification dispatcher yielding to receipts."""

    def __init__(self, repo: LedgerRepository, bot: Bot) -> None:
        self.repo = repo
        self.bot = bot
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def wake(self) -> None:
        self._wake.set()

    def start(self) -> None:
        self.repo.recover_announcements_on_startup()
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="ledgerflow-announcements")

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

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.repo.has_due_receipts():
                    await asyncio.sleep(0.5)
                    continue
                if await self.step_once():
                    await asyncio.sleep(ANNOUNCE_PACE_SECONDS)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("AnnouncementDispatcher loop error")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass

    async def _pick_live_link(self, owner_id: int) -> str | None:
        for link_id, enabled in self.repo.list_owner_links(owner_id, limit=8):
            if not enabled:
                continue
            try:
                info = await self.bot.get_business_connection(link_id)
                if info.user.id != owner_id or not info.is_enabled:
                    self.repo.upsert_link(link_id, info.user.id, bool(info.is_enabled))
                    continue
                rights = getattr(info, "rights", None)
                if rights is not None and getattr(rights, "can_reply", True) is False:
                    continue
                return link_id
            except (TelegramBadRequest, TelegramForbiddenError):
                self.repo.upsert_link(link_id, owner_id, False)
            except TelegramNetworkError:
                return link_id
            except TelegramAPIError:
                continue
        return None

    async def step_once(self) -> bool:
        claimed = self.repo.claim_next_delivery()
        if not claimed:
            return False
        job, dlv = claimed
        link_id = await self._pick_live_link(job.owner_id)
        if not link_id:
            self.repo.finish_delivery(dlv.id, "failed", error_text="No active Business connection")
            return True

        try:
            sent = await self.bot.send_message(
                chat_id=dlv.chat_id,
                text=job.body_text,
                business_connection_id=link_id,
                disable_notification=True,
            )
            self.repo.finish_delivery(dlv.id, "sent", tg_msg_id=sent.message_id)
        except TelegramRetryAfter as exc:
            delay = max(float(exc.retry_after), 1.0)
            if dlv.attempts < MAX_RETRY_AFTER:
                self.repo.finish_delivery(dlv.id, "pending", error_text=f"RetryAfter {delay}s", delay_seconds=delay)
            else:
                self.repo.finish_delivery(dlv.id, "failed", error_text=f"RetryAfter exhausted: {exc}")
        except TelegramNetworkError as exc:
            self.repo.finish_delivery(dlv.id, "uncertain", error_text=f"Uncertain network delivery: {exc}")
        except (TelegramBadRequest, TelegramForbiddenError, TelegramAPIError) as exc:
            self.repo.finish_delivery(dlv.id, "failed", error_text=f"{type(exc).__name__}: {exc}")
        return True
