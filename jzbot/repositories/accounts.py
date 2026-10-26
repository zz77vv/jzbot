from __future__ import annotations

import logging

from jzbot.domain import utc_now_iso
from jzbot.repositories.base import BaseRepository

logger = logging.getLogger("jzbot")


class AccountsRepository(BaseRepository):
    async def is_business_owner(self, owner_user_id: int) -> bool:
        conn = self._require_conn()
        row = await (
            await conn.execute(
                "SELECT 1 FROM business_accounts WHERE owner_user_id = ?",
                (owner_user_id,),
            )
        ).fetchone()
        return row is not None

    async def upsert_business_connection(
        self, connection_id: str, owner_user_id: int, is_enabled: bool
    ) -> bool:
        """
        返回 True 表示该 Business 用户从未发送过欢迎消息。
        是否真的发送成功由上层决定，成功后再 mark_welcome_sent。
        """
        conn = self._require_conn()
        now = utc_now_iso()
        async with self.write_lock:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await conn.execute(
                        """
                    SELECT welcome_sent
                    FROM business_accounts
                    WHERE owner_user_id = ?
                    """,
                        (owner_user_id,),
                    )
                ).fetchone()
                if row is None:
                    await conn.execute(
                        """
                    INSERT INTO business_accounts(
                        owner_user_id, welcome_sent, first_seen_at, last_seen_at
                    )
                    VALUES (?, 0, ?, ?)
                    """,
                        (owner_user_id, now, now),
                    )
                    welcome_needed = True
                else:
                    welcome_needed = not bool(row["welcome_sent"])
                    await conn.execute(
                        """
                    UPDATE business_accounts
                    SET last_seen_at = ?
                    WHERE owner_user_id = ?
                    """,
                        (now, owner_user_id),
                    )
                await conn.execute(
                    """
                INSERT INTO business_connections(
                    connection_id, owner_user_id, is_enabled, updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(connection_id) DO UPDATE SET
                    owner_user_id = excluded.owner_user_id,
                    is_enabled = excluded.is_enabled,
                    updated_at = excluded.updated_at
                """,
                    (connection_id, owner_user_id, int(is_enabled), now),
                )
                await conn.commit()
                return welcome_needed
            except Exception:
                await conn.rollback()
                raise

    async def mark_welcome_sent(self, owner_user_id: int) -> None:
        conn = self._require_conn()
        async with self.write_lock:
            await conn.execute(
                """
            UPDATE business_accounts
            SET welcome_sent = 1, last_seen_at = ?
            WHERE owner_user_id = ?
            """,
                (utc_now_iso(), owner_user_id),
            )
            await conn.commit()

    async def get_connection_owner(self, connection_id: str):
        conn = self._require_conn()
        row = await (
            await conn.execute(
                """
            SELECT owner_user_id, is_enabled
            FROM business_connections
            WHERE connection_id = ?
            """,
                (connection_id,),
            )
        ).fetchone()
        if row is None:
            return None
        return (int(row["owner_user_id"]), bool(row["is_enabled"]))

    async def get_enabled_connection_ids(self, owner_user_id: int) -> list[str]:
        """返回该 Business 用户当前已启用的连接，最近更新的排在前面。"""
        conn = self._require_conn()
        rows = await (
            await conn.execute(
                """
            SELECT connection_id
            FROM business_connections
            WHERE owner_user_id = ?
              AND is_enabled = 1
            ORDER BY updated_at DESC
            """,
                (owner_user_id,),
            )
        ).fetchall()
        return [str(row["connection_id"]) for row in rows]

    async def mark_business_connection_disabled(
        self, connection_id: str, owner_user_id: int
    ) -> None:
        """Disable one confirmed-invalid connection without deleting its history."""
        conn = self._require_conn()
        async with self.write_lock:
            await conn.execute(
                """
            UPDATE business_connections
            SET is_enabled = 0, updated_at = ?
            WHERE connection_id = ? AND owner_user_id = ?
            """,
                (utc_now_iso(), connection_id, owner_user_id),
            )
            await conn.commit()
