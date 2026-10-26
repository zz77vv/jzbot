"""Bounded, confirmed connection recovery; no receipt delivery in this service."""

import logging
import secrets
from datetime import datetime, timedelta, timezone

from telegram.error import TelegramError

logger = logging.getLogger("jzbot")


class ReceiptRecoveryService:
    def __init__(self, store):
        self.store = store

    async def _refresh(self, context, owner, connection_id):
        connection = await context.bot.get_business_connection(connection_id)
        if connection.user.id != owner:
            raise PermissionError("Connection owner mismatch")
        await self.store.upsert_business_connection(
            connection.id, owner, connection.is_enabled
        )
        return (
            connection.is_enabled
            and getattr(getattr(connection, "rights", None), "can_reply", None)
            is not False
        )

    async def prepare(self, context, owner, outbox_id, page=0, *, now=None):
        if not await self.store.is_business_owner(owner):
            raise PermissionError("Business owner required")
        row = await self.store.get_owned_outbox(owner, outbox_id)
        if row is None or row["status"] != "pending":
            return None
        ids = [
            row["business_connection_id"]
        ] + await self.store.get_enabled_connection_ids(owner)
        for connection_id in list(dict.fromkeys(ids))[:8]:
            try:
                if not await self._refresh(context, owner, connection_id):
                    continue
            except TelegramError as exc:
                logger.warning(
                    "Receipt connection check | owner=%s outbox=%s exception_type=%s",
                    owner,
                    outbox_id,
                    type(exc).__name__,
                )
                continue
            now = now or datetime.now(timezone.utc)
            return dict(
                nonce=secrets.token_hex(12),
                owner=owner,
                outbox_id=outbox_id,
                attempt_count=row["attempt_count"],
                previous_connection_id=row["business_connection_id"],
                connection_id=connection_id,
                expires=(now + timedelta(minutes=5)).isoformat(),
                page=max(0, min(page, 999999)),
            )
        return None

    async def confirm(self, context, owner, request, *, now=None):
        if request["owner"] != owner or not await self.store.is_business_owner(owner):
            raise PermissionError("Business owner required")
        if (now or datetime.now(timezone.utc)) >= datetime.fromisoformat(
            request["expires"]
        ):
            return "expired"
        async with self.store.delivery_lock:
            if not await self._refresh(context, owner, request["connection_id"]):
                return "unavailable"
            result = await self.store.recover_receipt_connection(owner, request)
            logger.info(
                "Receipt recovery | owner=%s outbox=%s result=%s; schedule/state retained",
                owner,
                request["outbox_id"],
                result,
            )
            return result
