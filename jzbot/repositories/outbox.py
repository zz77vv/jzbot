from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiosqlite
from telegram.error import RetryAfter

from jzbot.domain import (
    ADMIN_PAGE_SIZE,
    OUTBOX_ATTENTION_ATTEMPTS,
    OUTBOX_BATCH_SIZE,
    admin_page_bounds,
    outbox_attention_cutoff,
    outbox_backoff_seconds,
    utc_after_iso,
    utc_now_iso,
)
from jzbot.infra.logging import redact
from jzbot.repositories.base import BaseRepository

logger = logging.getLogger("jzbot")


class OutboxRepository(BaseRepository):
    async def get_owned_outbox(self, owner, outbox_id):
        return await (
            await self._require_conn().execute(
                "SELECT * FROM receipt_outbox WHERE owner_user_id=? AND id=?",
                (owner, outbox_id),
            )
        ).fetchone()

    async def get_pending_receipts_page(self, owner, page=0):
        conn = self._require_conn()
        total = (
            await (
                await conn.execute(
                    "SELECT COUNT(*) FROM receipt_outbox WHERE owner_user_id=? AND status='pending'",
                    (owner,),
                )
            ).fetchone()
        )[0]
        page, _ = admin_page_bounds(total, page)
        rows = await (
            await conn.execute(
                """
            SELECT o.*,p.username,p.first_name,p.last_name
            FROM receipt_outbox o LEFT JOIN customer_profiles p
              ON p.owner_user_id=o.owner_user_id AND p.customer_chat_id=o.customer_chat_id
            WHERE o.owner_user_id=? AND o.status='pending'
            ORDER BY o.id DESC LIMIT ? OFFSET ?
        """,
                (owner, ADMIN_PAGE_SIZE, page * ADMIN_PAGE_SIZE),
            )
        ).fetchall()
        return list(rows), total, page

    async def recover_receipt_connection(self, owner, request):
        """Only rebind a still-pending task; never force-send, reset or mark success."""
        conn = self._require_conn()
        async with self.write_lock:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                row = await self.get_owned_outbox(owner, request["outbox_id"])
                connection = await self.store.get_connection_owner(
                    request["connection_id"]
                )
                allowed = await self.store.is_business_owner(owner)
                if not allowed or connection != (owner, True):
                    raise PermissionError(
                        "Current owner and enabled connection required"
                    )
                if (
                    row is None
                    or row["status"] != "pending"
                    or row["attempt_count"] != request["attempt_count"]
                    or row["business_connection_id"]
                    != request["previous_connection_id"]
                ):
                    await conn.rollback()
                    return "stale"
                await conn.execute(
                    """
                    UPDATE receipt_outbox SET business_connection_id=?
                    WHERE owner_user_id=? AND id=? AND status='pending'
                """,
                    (request["connection_id"], owner, request["outbox_id"]),
                )
                await conn.commit()
                return (
                    "checked"
                    if request["connection_id"] == request["previous_connection_id"]
                    else "rebound"
                )
            except BaseException:
                await conn.rollback()
                raise

    async def _insert_receipt_outbox(
        self,
        conn: aiosqlite.Connection,
        *,
        owner_user_id: int,
        customer_chat_id: int,
        source_message_id: int,
        business_connection_id: str,
        receipt_type: str,
        receipt_text: str,
        should_pin: bool,
        now: str,
    ) -> int:
        cursor = await conn.execute(
            """
        INSERT INTO receipt_outbox(
            owner_user_id,
            customer_chat_id,
            source_message_id,
            business_connection_id,
            receipt_type,
            receipt_text,
            reply_to_message_id,
            should_pin,
            status,
            attempt_count,
            created_at_utc,
            next_attempt_at_utc
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
        """,
            (
                owner_user_id,
                customer_chat_id,
                source_message_id,
                business_connection_id,
                receipt_type,
                receipt_text,
                source_message_id,
                int(should_pin),
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)

    async def get_outbox_for_source(
        self, owner_user_id: int, customer_chat_id: int, source_message_id: int
    ):
        conn = self._require_conn()
        return await (
            await conn.execute(
                """
            SELECT *
            FROM receipt_outbox
            WHERE owner_user_id = ?
              AND customer_chat_id = ?
              AND source_message_id = ?
            ORDER BY id ASC
            LIMIT 1
            """,
                (owner_user_id, customer_chat_id, source_message_id),
            )
        ).fetchone()

    async def get_outbox(self, outbox_id: int):
        conn = self._require_conn()
        return await (
            await conn.execute(
                "SELECT * FROM receipt_outbox WHERE id = ?", (outbox_id,)
            )
        ).fetchone()

    async def get_due_outbox(
        self, limit: int = OUTBOX_BATCH_SIZE, now_utc: str | None = None
    ):
        conn = self._require_conn()
        return await (
            await conn.execute(
                """
            SELECT *
            FROM receipt_outbox
            WHERE status = 'pending'
              AND next_attempt_at_utc <= ?
            ORDER BY next_attempt_at_utc ASC, id ASC
            LIMIT ?
            """,
                (now_utc or utc_now_iso(), limit),
            )
        ).fetchall()

    async def has_newer_delivered_pin(
        self, owner_user_id: int, customer_chat_id: int, outbox_id: int
    ) -> bool:
        """Receipt IDs follow committed accounting order, independent of retries."""
        row = await (
            await self._require_conn().execute(
                """
            SELECT 1 FROM receipt_outbox
            WHERE owner_user_id = ? AND customer_chat_id = ? AND id > ?
              AND status = 'delivered' AND should_pin = 1
            LIMIT 1
            """,
                (owner_user_id, customer_chat_id, outbox_id),
            )
        ).fetchone()
        return row is not None

    async def mark_outbox_delivered(
        self, outbox_id: int, delivered_at_utc: str | None = None
    ) -> None:
        conn = self._require_conn()
        async with self.write_lock:
            await conn.execute(
                """
            UPDATE receipt_outbox
            SET status = 'delivered',
                delivered_at_utc = ?,
                last_error = NULL
            WHERE id = ? AND status = 'pending'
            """,
                (delivered_at_utc or utc_now_iso(), outbox_id),
            )
            await conn.commit()

    async def mark_outbox_failed(
        self, outbox_id: int, error: BaseException, now: datetime | None = None
    ) -> None:
        conn = self._require_conn()
        async with self.write_lock:
            row = await (
                await conn.execute(
                    """
                SELECT attempt_count
                FROM receipt_outbox
                WHERE id = ? AND status = 'pending'
                """,
                    (outbox_id,),
                )
            ).fetchone()
            if row is None:
                return
            attempt_count = int(row["attempt_count"]) + 1
            delay = outbox_backoff_seconds(attempt_count)
            if isinstance(error, RetryAfter):
                wait = error.retry_after
                if hasattr(wait, "total_seconds"):
                    wait = wait.total_seconds()
                delay = max(delay, int(wait) + 1)
            retry_at = utc_after_iso(delay, now=now)
            await conn.execute(
                """
            UPDATE receipt_outbox
            SET attempt_count = ?,
                next_attempt_at_utc = ?,
                last_error = ?
            WHERE id = ? AND status = 'pending'
            """,
                (attempt_count, retry_at, redact(str(error))[:500], outbox_id),
            )
            await conn.commit()

    async def get_outbox_attention_count(
        self, owner_user_id: int, now: datetime | None = None
    ) -> int:
        conn = self._require_conn()
        cutoff = outbox_attention_cutoff(now)
        row = await (
            await conn.execute(
                "SELECT COUNT(*) AS total FROM receipt_outbox WHERE owner_user_id = ? AND status = 'pending' AND (attempt_count >= ? OR created_at_utc <= ?)",
                (owner_user_id, OUTBOX_ATTENTION_ATTEMPTS, cutoff),
            )
        ).fetchone()
        return int(row["total"])

    async def get_outbox_attention_page(
        self, owner_user_id: int, page: int = 0, now: datetime | None = None
    ):
        """Read-only attention view; neither delivery status nor retries change."""
        now = now or datetime.now(timezone.utc)
        total = await self.get_outbox_attention_count(owner_user_id, now)
        page, _ = admin_page_bounds(total, page)
        rows = await (
            await self._require_conn().execute(
                "SELECT o.customer_chat_id, o.attempt_count, o.created_at_utc, o.last_error, p.username, p.first_name, p.last_name FROM receipt_outbox AS o LEFT JOIN customer_profiles AS p ON p.owner_user_id = o.owner_user_id AND p.customer_chat_id = o.customer_chat_id WHERE o.owner_user_id = ? AND o.status = 'pending' AND (o.attempt_count >= ? OR o.created_at_utc <= ?) ORDER BY o.id DESC LIMIT ? OFFSET ?",
                (
                    owner_user_id,
                    OUTBOX_ATTENTION_ATTEMPTS,
                    outbox_attention_cutoff(now),
                    ADMIN_PAGE_SIZE,
                    page * ADMIN_PAGE_SIZE,
                ),
            )
        ).fetchall()
        return (list(rows), total, page)

