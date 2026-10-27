from __future__ import annotations

import logging

from jzbot.domain import ADMIN_PAGE_SIZE, normalize_admin_search, utc_now_iso
from jzbot.repositories.base import BaseRepository

logger = logging.getLogger("jzbot")


class CustomersRepository(BaseRepository):
    async def get_broadcast_customer_rows(self, owner_user_id, selected, limit):
        """Owner-scoped union, indexed ledger existence and exact integer signs."""
        if not selected or not set(selected) <= {'all', 'deposit', 'debt'}:
            raise ValueError('Invalid broadcast audience')
        clauses = []
        if 'all' in selected:
            clauses.extend([
                "EXISTS(SELECT 1 FROM ledger l WHERE l.owner_user_id=w.owner_user_id AND l.customer_chat_id=w.customer_chat_id)",
                "EXISTS(SELECT 1 FROM admin_operations a WHERE a.owner_user_id=w.owner_user_id AND a.customer_chat_id=w.customer_chat_id AND a.result='applied')",
            ])
        if 'deposit' in selected:
            clauses.append("substr(w.balance_tenths,1,1)!='-'")
        if 'debt' in selected:
            clauses.append("substr(w.balance_tenths,1,1)='-'")
        return await (await self._require_conn().execute(
            """SELECT w.customer_chat_id,p.first_name,p.last_name,p.username
            FROM wallets w LEFT JOIN customer_profiles p
            ON p.owner_user_id=w.owner_user_id AND p.customer_chat_id=w.customer_chat_id
            WHERE w.owner_user_id=? AND (""" + ' OR '.join(clauses) + ") ORDER BY w.customer_chat_id LIMIT ?",
            (owner_user_id, limit),
        )).fetchall()

    async def get_deposit_wallet_rows(self, owner_user_id):
        """Historical deposits, independent of the current positive-balance filter."""
        return await (
            await self._require_conn().execute(
                """
            SELECT w.customer_chat_id,w.balance_tenths,w.created_at,w.updated_at,
                   p.username,p.first_name,p.last_name,COALESCE(m.is_disabled,0) AS is_disabled
            FROM wallets w JOIN wallet_stats s
                ON s.owner_user_id=w.owner_user_id AND s.customer_chat_id=w.customer_chat_id
            LEFT JOIN customer_profiles p
                ON p.owner_user_id=w.owner_user_id AND p.customer_chat_id=w.customer_chat_id
            LEFT JOIN customer_management m
                ON m.owner_user_id=w.owner_user_id AND m.customer_chat_id=w.customer_chat_id
            WHERE w.owner_user_id=? AND s.deposit_total_tenths != '0'
            ORDER BY w.updated_at DESC,w.customer_chat_id ASC
        """,
                (owner_user_id,),
            )
        ).fetchall()

    async def upsert_customer_profile(
        self,
        owner_user_id: int,
        customer_chat_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> None:
        conn = self._require_conn()
        now = utc_now_iso()
        async with self.write_lock:
            await conn.execute(
                """
            INSERT INTO customer_profiles(
                owner_user_id,
                customer_chat_id,
                username,
                first_name,
                last_name,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner_user_id, customer_chat_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                updated_at = excluded.updated_at
            """,
                (owner_user_id, customer_chat_id, username, first_name, last_name, now),
            )
            await conn.commit()

    async def get_wallet_rows(self, owner_user_id: int):
        """
        余额存 TEXT 是为了支持任意大的 Python int。
        因此统计和排序放在 Python 做，避免 SQLite SUM/CAST 溢出或精度丢失。
        """
        conn = self._require_conn()
        return await (
            await conn.execute(
                """
            SELECT
                w.customer_chat_id,
                w.balance_tenths,
                w.created_at,
                w.updated_at,
                p.username,
                p.first_name,
                p.last_name,
                COALESCE(m.is_disabled, 0) AS is_disabled
            FROM wallets AS w
            LEFT JOIN customer_profiles AS p
              ON p.owner_user_id = w.owner_user_id
             AND p.customer_chat_id = w.customer_chat_id
            LEFT JOIN customer_management AS m
              ON m.owner_user_id = w.owner_user_id
             AND m.customer_chat_id = w.customer_chat_id
            WHERE w.owner_user_id = ?
            """,
                (owner_user_id,),
            )
        ).fetchall()

    async def search_customers(
        self,
        owner_user_id: int,
        query: str,
        limit: int = ADMIN_PAGE_SIZE,
        offset: int = 0,
    ):
        """Search this owner's wallet/profile rows with case-insensitive LIKE."""
        conn = self._require_conn()
        normalized = normalize_admin_search(query).casefold()
        if not normalized:
            return ([], 0)
        escaped = (
            normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        pattern = f"%{escaped}%"
        where_sql = "\n        w.owner_user_id = ?\n        AND (\n            LOWER(COALESCE(p.username, '')) LIKE ? ESCAPE '\\'\n            OR LOWER(COALESCE(p.first_name, '')) LIKE ? ESCAPE '\\'\n            OR LOWER(COALESCE(p.last_name, '')) LIKE ? ESCAPE '\\'\n            OR LOWER(TRIM(\n                COALESCE(p.first_name, '') || ' ' || COALESCE(p.last_name, '')\n            )) LIKE ? ESCAPE '\\'\n            OR CAST(w.customer_chat_id AS TEXT) LIKE ? ESCAPE '\\'\n        )\n    "
        params = (owner_user_id, pattern, pattern, pattern, pattern, pattern)
        total_row = await (
            await conn.execute(
                f"\n            SELECT COUNT(*) AS total\n            FROM wallets AS w\n            LEFT JOIN customer_profiles AS p\n              ON p.owner_user_id = w.owner_user_id\n             AND p.customer_chat_id = w.customer_chat_id\n            WHERE {where_sql}\n            ",
                params,
            )
        ).fetchone()
        rows = await (
            await conn.execute(
                f"\n            SELECT\n                w.customer_chat_id,\n                w.balance_tenths,\n                w.created_at,\n                w.updated_at,\n                p.username,\n                p.first_name,\n                p.last_name,\n                COALESCE(m.is_disabled, 0) AS is_disabled\n            FROM wallets AS w\n            LEFT JOIN customer_profiles AS p\n              ON p.owner_user_id = w.owner_user_id\n             AND p.customer_chat_id = w.customer_chat_id\n            LEFT JOIN customer_management AS m\n              ON m.owner_user_id = w.owner_user_id\n             AND m.customer_chat_id = w.customer_chat_id\n            WHERE {where_sql}\n            ORDER BY w.updated_at DESC, w.customer_chat_id ASC\n            LIMIT ? OFFSET ?\n            ",
                (*params, limit, max(0, offset)),
            )
        ).fetchall()
        return (list(rows), int(total_row["total"]))

    async def get_customer_detail(self, owner_user_id: int, customer_chat_id: int):
        """快速读取客户详情，不再扫描该客户的全部历史流水。"""
        conn = self._require_conn()
        wallet = await (
            await conn.execute(
                """
            SELECT
                w.customer_chat_id,
                w.balance_tenths,
                w.created_at,
                w.updated_at,
                p.username,
                p.first_name,
                p.last_name,
                COALESCE(m.is_disabled, 0) AS is_disabled
            FROM wallets AS w
            LEFT JOIN customer_profiles AS p
              ON p.owner_user_id = w.owner_user_id
             AND p.customer_chat_id = w.customer_chat_id
            LEFT JOIN customer_management AS m
              ON m.owner_user_id = w.owner_user_id
             AND m.customer_chat_id = w.customer_chat_id
            WHERE w.owner_user_id = ?
              AND w.customer_chat_id = ?
            """,
                (owner_user_id, customer_chat_id),
            )
        ).fetchone()
        if wallet is None:
            return None
        stats = await (
            await conn.execute(
                """
            SELECT
                deposit_total_tenths,
                withdraw_total_tenths,
                entry_count
            FROM wallet_stats
            WHERE owner_user_id = ?
              AND customer_chat_id = ?
            """,
                (owner_user_id, customer_chat_id),
            )
        ).fetchone()
        if stats is None:
            deposit_total = 0
            withdraw_total = 0
            entry_count = 0
        else:
            deposit_total = int(stats["deposit_total_tenths"])
            withdraw_total = int(stats["withdraw_total_tenths"])
            entry_count = int(stats["entry_count"])
        recent = await self.store.get_recent_entries(
            owner_user_id, customer_chat_id, limit=5, oldest_first=False
        )
        return {
            "wallet": wallet,
            "deposit_total": deposit_total,
            "withdraw_total": withdraw_total,
            "entry_count": entry_count,
            "recent": recent,
        }
