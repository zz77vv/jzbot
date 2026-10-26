from __future__ import annotations

import logging
import time

from jzbot.domain import utc_now_iso

logger = logging.getLogger("jzbot")


class LegacyStatsMigration:
    async def _ensure_wallet_stats_migrated(self) -> None:
        conn = self._require_conn()

        ready = await (
            await conn.execute(
                "SELECT meta_value FROM app_meta WHERE meta_key = 'wallet_stats_v1'"
            )
        ).fetchone()

        if ready is not None and ready["meta_value"] == "1":
            return

        logger.info("Building wallet_stats from existing ledger...")
        started = time.perf_counter()

        # 逐行读取，不把全部历史流水一次性装入内存。
        cursor = await conn.execute(
            """
            SELECT owner_user_id, customer_chat_id, entry_type, amount_tenths
            FROM ledger
            ORDER BY owner_user_id, customer_chat_id, id ASC
            """
        )

        stats: dict[tuple[int, int], list[int]] = {}
        async for row in cursor:
            key = (int(row["owner_user_id"]), int(row["customer_chat_id"]))
            current = stats.setdefault(key, [0, 0, 0])

            entry_type = row["entry_type"]
            amount = int(row["amount_tenths"])
            if entry_type == "deposit":
                current[0] += amount
            elif entry_type == "withdraw":
                current[1] += amount

            # 和旧版行为保持一致：clear 也算一次记账。
            current[2] += 1

        await cursor.close()

        now = utc_now_iso()
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await conn.execute("DELETE FROM wallet_stats")

            if stats:
                await conn.executemany(
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
                    """,
                    [
                        (owner, chat, str(values[0]), str(values[1]), values[2], now)
                        for (owner, chat), values in stats.items()
                    ],
                )

            # 没有历史流水但已经存在 wallet 的客户，也补一行 0 统计。
            await conn.execute(
                """
                INSERT OR IGNORE INTO wallet_stats(
                    owner_user_id,
                    customer_chat_id,
                    deposit_total_tenths,
                    withdraw_total_tenths,
                    entry_count,
                    updated_at
                )
                SELECT owner_user_id, customer_chat_id, '0', '0', 0, ?
                FROM wallets
                """,
                (now,),
            )

            await conn.execute(
                """
                INSERT INTO app_meta(meta_key, meta_value)
                VALUES ('wallet_stats_v1', '1')
                ON CONFLICT(meta_key) DO UPDATE SET meta_value = excluded.meta_value
                """
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

        logger.info(
            "wallet_stats ready | customers=%s elapsed=%.3fs",
            len(stats),
            time.perf_counter() - started,
        )
