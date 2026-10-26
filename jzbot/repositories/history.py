from __future__ import annotations

import logging

from jzbot.domain import ADMIN_PAGE_SIZE, admin_page_bounds
from jzbot.repositories.base import BaseRepository

logger = logging.getLogger("jzbot")


class HistoryRepository(BaseRepository):
    async def _admin_clear_count(self, owner: int, customer: int) -> int:
        row = await (
            await self._require_conn().execute(
                """
        SELECT COUNT(*) AS total FROM admin_operations
        WHERE owner_user_id=? AND customer_chat_id=? AND result='applied'
    """,
                (owner, customer),
            )
        ).fetchone()
        return int(row["total"])

    async def _combined_entries(
        self, owner: int, customer: int, limit: int, offset: int = 0
    ):
        """Bound each indexed source in SQL, then merge in SQL, never Python slicing.

        The ledger watermark orders admin clears after their preceding customer
        entry and before later customer entries, even within the same second.
        """
        take = max(0, limit + offset)
        return await (
            await self._require_conn().execute(
                """
        WITH business AS (
            SELECT id, entry_type, amount_tenths, balance_before, balance_after,
                   created_at_utc, source_message_id
            FROM ledger WHERE owner_user_id=? AND customer_chat_id=?
            ORDER BY id DESC LIMIT ?
        ), administrative AS (
            SELECT * FROM admin_operations
            WHERE owner_user_id=? AND customer_chat_id=? AND result='applied'
            ORDER BY ledger_anchor_id DESC, id DESC LIMIT ?
        )
        SELECT entry_type, amount_tenths, balance_before, balance_after, created_at_utc,
            source_message_id, 'business' AS operation_origin, NULL AS operation_id,
            id AS ledger_order, 0 AS origin_order, id AS action_order
        FROM business
        UNION ALL
        SELECT entry_type,
            CASE WHEN substr(balance_before,1,1)='-' THEN substr(balance_before,2)
                 ELSE balance_before END,
            balance_before, balance_after, completed_at_utc, NULL, 'admin', operation_id,
            ledger_anchor_id, 1, id
        FROM administrative
        ORDER BY ledger_order DESC, origin_order DESC, action_order DESC LIMIT ? OFFSET ?
    """,
                (owner, customer, take, owner, customer, take, limit, offset),
            )
        ).fetchall()

    async def get_recent_entries(
        self,
        owner_user_id: int,
        customer_chat_id: int,
        limit: int = 5,
        oldest_first: bool = True,
    ):
        conn = self._require_conn()
        if await self._admin_clear_count(owner_user_id, customer_chat_id):
            rows = list(
                await self._combined_entries(owner_user_id, customer_chat_id, limit)
            )
            return list(reversed(rows)) if oldest_first else rows
        rows = await (
            await conn.execute(
                """
            SELECT
                entry_type,
                amount_tenths,
                balance_before,
                balance_after,
                created_at_utc
            FROM ledger
            WHERE owner_user_id = ?
              AND customer_chat_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
                (owner_user_id, customer_chat_id, limit),
            )
        ).fetchall()
        if oldest_first:
            return list(reversed(rows))
        return list(rows)

    async def get_balance(self, owner_user_id: int, customer_chat_id: int) -> int:
        conn = self._require_conn()
        row = await (
            await conn.execute(
                """
            SELECT balance_tenths
            FROM wallets
            WHERE owner_user_id = ?
              AND customer_chat_id = ?
            """,
                (owner_user_id, customer_chat_id),
            )
        ).fetchone()
        return int(row["balance_tenths"]) if row else 0

    async def get_ledger_page(
        self, owner_user_id: int, customer_chat_id: int, page: int = 0
    ):
        """Read one owner's history using the existing idx_ledger_recent index."""
        conn = self._require_conn()
        total_row = await (
            await conn.execute(
                "SELECT COUNT(*) AS total FROM ledger WHERE owner_user_id = ? AND customer_chat_id = ?",
                (owner_user_id, customer_chat_id),
            )
        ).fetchone()
        total = int(total_row["total"])
        administrative = await self._admin_clear_count(owner_user_id, customer_chat_id)
        if administrative:
            total += administrative
            page, _ = admin_page_bounds(total, page)
            rows = await self._combined_entries(
                owner_user_id, customer_chat_id, ADMIN_PAGE_SIZE, page * ADMIN_PAGE_SIZE
            )
            return (list(rows), total, page)
        page, _ = admin_page_bounds(total, page)
        rows = await (
            await conn.execute(
                "SELECT entry_type, amount_tenths, balance_before, balance_after, created_at_utc FROM ledger WHERE owner_user_id = ? AND customer_chat_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                (
                    owner_user_id,
                    customer_chat_id,
                    ADMIN_PAGE_SIZE,
                    page * ADMIN_PAGE_SIZE,
                ),
            )
        ).fetchall()
        return (list(rows), total, page)
