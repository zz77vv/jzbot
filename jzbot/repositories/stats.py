from __future__ import annotations

import logging

import aiosqlite

from jzbot.repositories.base import BaseRepository

logger = logging.getLogger("jzbot")


class StatsRepository(BaseRepository):
    async def _get_stats_for_update(
        self, conn: aiosqlite.Connection, owner_user_id: int, customer_chat_id: int
    ) -> tuple[int, int, int]:
        row = await (
            await conn.execute(
                """
            SELECT deposit_total_tenths, withdraw_total_tenths, entry_count
            FROM wallet_stats
            WHERE owner_user_id = ?
              AND customer_chat_id = ?
            """,
                (owner_user_id, customer_chat_id),
            )
        ).fetchone()
        if row is None:
            return (0, 0, 0)
        return (
            int(row["deposit_total_tenths"]),
            int(row["withdraw_total_tenths"]),
            int(row["entry_count"]),
        )

    async def _write_stats(
        self,
        conn: aiosqlite.Connection,
        owner_user_id: int,
        customer_chat_id: int,
        deposit_total: int,
        withdraw_total: int,
        entry_count: int,
        now: str,
    ) -> None:
        await conn.execute(
            """
        INSERT INTO wallet_stats(
            owner_user_id,
            customer_chat_id,
            deposit_total_tenths,
            withdraw_total_tenths,
            entry_count,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(owner_user_id, customer_chat_id) DO UPDATE SET
            deposit_total_tenths = excluded.deposit_total_tenths,
            withdraw_total_tenths = excluded.withdraw_total_tenths,
            entry_count = excluded.entry_count,
            updated_at = excluded.updated_at
        """,
            (
                owner_user_id,
                customer_chat_id,
                str(deposit_total),
                str(withdraw_total),
                entry_count,
                now,
            ),
        )
