from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from telegram import (
    ReplyParameters,
)
from telegram.ext import Application

from jzbot.domain import OUTBOX_BATCH_SIZE, OUTBOX_POLL_SECONDS
from jzbot.infra.business import try_pin_business_message

logger = logging.getLogger("jzbot")


class OutboxService:
    def __init__(self, store):
        self.store = store
        self.send_lock = store.delivery_lock
        self.wakeup = asyncio.Event()

    def notify(self):
        """Only wake the independent worker after a transaction has committed."""
        self.wakeup.set()

    async def deliver_outbox_receipt(
        self, context, outbox_id: int, *, now=None
    ) -> bool:
        """Try one durable receipt; accounting is already committed at this point."""
        async with self.send_lock:
            # aiosqlite shares one connection: never send an uncommitted row
            # observed while an accounting coroutine is suspended before COMMIT.
            async with self.store.write_lock:
                row = await self.store.get_outbox(outbox_id)
                if row is not None:
                    row = dict(row)
            if row is None:
                return False
            if row["status"] == "delivered":
                return True
            current = now or datetime.now(timezone.utc)
            if datetime.fromisoformat(row["next_attempt_at_utc"]) > current:
                return False
            kwargs = {
                "chat_id": int(row["customer_chat_id"]),
                "text": row["receipt_text"],
                "business_connection_id": row["business_connection_id"],
                "reply_parameters": ReplyParameters(
                    message_id=int(row["reply_to_message_id"])
                ),
            }
            try:
                sent = await context.bot.send_message(**kwargs)
            except Exception as exc:
                await self.store.mark_outbox_failed(outbox_id, exc)
                logger.warning(
                    "Receipt delivery deferred | owner=%s customer=%s source=%s outbox=%s error=%s",
                    row["owner_user_id"],
                    row["customer_chat_id"],
                    row["source_message_id"],
                    outbox_id,
                    exc,
                )
                return False
            try:
                await self.store.mark_outbox_delivered(outbox_id)
            except Exception:
                logger.exception(
                    "Receipt delivered but acknowledgement is ambiguous | owner=%s customer=%s source=%s outbox=%s",
                    row["owner_user_id"],
                    row["customer_chat_id"],
                    row["source_message_id"],
                    outbox_id,
                )
                return False
            if bool(row["should_pin"]):
                # Keep historical receipt text and replies, but never let a
                # delayed receipt replace a newer balance's pin. The shared
                # delivery lock covers this check through the Telegram call.
                async with self.store.write_lock:
                    superseded = await self.store.has_newer_delivered_pin(
                        row["owner_user_id"], row["customer_chat_id"], outbox_id
                    )
                if not superseded:
                    await try_pin_business_message(
                        context,
                        row["business_connection_id"],
                        int(row["customer_chat_id"]),
                        sent.message_id,
                    )
            return True

    async def process_due_outbox(self, context, limit: int = OUTBOX_BATCH_SIZE) -> int:
        rows = await self.store.get_due_outbox(limit=limit)
        delivered = 0
        for row in rows:
            if await self.deliver_outbox_receipt(context, int(row["id"])):
                delivered += 1
        return delivered

    async def outbox_worker(self, application: Application) -> None:
        stop_event = application.bot_data["outbox_stop_event"]
        try:
            while not stop_event.is_set():
                self.wakeup.clear()
                try:
                    await self.process_due_outbox(application)
                except Exception:
                    logger.exception("Outbox worker iteration failed")
                waits = [
                    asyncio.create_task(stop_event.wait()),
                    asyncio.create_task(self.wakeup.wait()),
                ]
                try:
                    await asyncio.wait(
                        waits,
                        timeout=OUTBOX_POLL_SECONDS,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for task in waits:
                        task.cancel()
                    await asyncio.gather(*waits, return_exceptions=True)
        except asyncio.CancelledError:
            raise

