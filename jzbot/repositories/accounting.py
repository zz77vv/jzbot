from __future__ import annotations

import logging

from jzbot.domain import format_tenths, utc_now_iso
from jzbot.repositories.base import BaseRepository

logger = logging.getLogger("jzbot")


class AccountingRepository(BaseRepository):
    async def apply_amount(
        self,
        owner_user_id: int,
        customer_chat_id: int,
        source_message_id: int,
        delta_tenths: int,
        business_connection_id: str,
    ):
        """原子执行入金/出金。"""
        if type(delta_tenths) is not int or not delta_tenths:
            raise ValueError("Amount must be a nonzero integer number of tenths")
        conn = self._require_conn()
        now = utc_now_iso()
        async with self.write_lock:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                duplicate = await (
                    await conn.execute(
                        """
                    SELECT 1
                    FROM processed_messages
                    WHERE owner_user_id = ?
                      AND customer_chat_id = ?
                      AND source_message_id = ?
                    """,
                        (owner_user_id, customer_chat_id, source_message_id),
                    )
                ).fetchone()
                if duplicate:
                    await conn.rollback()
                    outbox = await self.store.get_outbox_for_source(
                        owner_user_id, customer_chat_id, source_message_id
                    )
                    return {
                        "duplicate": True,
                        "outbox_id": int(outbox["id"]) if outbox else None,
                        "outbox_status": outbox["status"] if outbox else None,
                    }
                wallet = await (
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
                if wallet is None:
                    before = 0
                    await conn.execute(
                        """
                    INSERT INTO wallets(
                        owner_user_id,
                        customer_chat_id,
                        balance_tenths,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, '0', ?, ?)
                    """,
                        (owner_user_id, customer_chat_id, now, now),
                    )
                else:
                    before = int(wallet["balance_tenths"])
                after = before + delta_tenths
                entry_type = "deposit" if delta_tenths > 0 else "withdraw"
                absolute_amount = abs(delta_tenths)
                await conn.execute(
                    """
                UPDATE wallets
                SET balance_tenths = ?, updated_at = ?
                WHERE owner_user_id = ?
                  AND customer_chat_id = ?
                """,
                    (str(after), now, owner_user_id, customer_chat_id),
                )
                await conn.execute(
                    """
                INSERT INTO ledger(
                    owner_user_id,
                    customer_chat_id,
                    source_message_id,
                    entry_type,
                    amount_tenths,
                    balance_before,
                    balance_after,
                    created_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        owner_user_id,
                        customer_chat_id,
                        source_message_id,
                        entry_type,
                        str(absolute_amount),
                        str(before),
                        str(after),
                        now,
                    ),
                )
                (
                    deposit_total,
                    withdraw_total,
                    entry_count,
                ) = await self.store._get_stats_for_update(
                    conn, owner_user_id, customer_chat_id
                )
                if entry_type == "deposit":
                    deposit_total += absolute_amount
                else:
                    withdraw_total += absolute_amount
                await self.store._write_stats(
                    conn,
                    owner_user_id,
                    customer_chat_id,
                    deposit_total,
                    withdraw_total,
                    entry_count + 1,
                    now,
                )
                await conn.execute(
                    """
                INSERT INTO processed_messages(
                    owner_user_id,
                    customer_chat_id,
                    source_message_id,
                    command_type,
                    processed_at_utc
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                    (
                        owner_user_id,
                        customer_chat_id,
                        source_message_id,
                        entry_type,
                        now,
                    ),
                )
                action = "入金" if entry_type == "deposit" else "出金"
                receipt_text = (
                    f"{action} {format_tenths(absolute_amount)} USDT, "
                    f"余额 {format_tenths(after)} USDT"
                )
                outbox_id = await self.store._insert_receipt_outbox(
                    conn,
                    owner_user_id=owner_user_id,
                    customer_chat_id=customer_chat_id,
                    source_message_id=source_message_id,
                    business_connection_id=business_connection_id,
                    receipt_type="amount",
                    receipt_text=receipt_text,
                    should_pin=True,
                    now=now,
                )
                await conn.commit()
                return {
                    "duplicate": False,
                    "before": before,
                    "after": after,
                    "entry_type": entry_type,
                    "amount": absolute_amount,
                    "receipt_text": receipt_text,
                    "outbox_id": outbox_id,
                }
            except BaseException:
                await conn.rollback()
                raise

    async def clear_balance(
        self,
        owner_user_id: int,
        customer_chat_id: int,
        source_message_id: int,
        business_connection_id: str,
    ):
        """
        清账：
        - 当前余额 != 0：写入 clear 流水并归零
        - 当前余额 == 0 / 无账本：只作为已处理命令，不新增流水、不建空账本
        """
        conn = self._require_conn()
        now = utc_now_iso()
        async with self.write_lock:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                duplicate = await (
                    await conn.execute(
                        """
                    SELECT 1
                    FROM processed_messages
                    WHERE owner_user_id = ?
                      AND customer_chat_id = ?
                      AND source_message_id = ?
                    """,
                        (owner_user_id, customer_chat_id, source_message_id),
                    )
                ).fetchone()
                if duplicate:
                    await conn.rollback()
                    outbox = await self.store.get_outbox_for_source(
                        owner_user_id, customer_chat_id, source_message_id
                    )
                    return {
                        "duplicate": True,
                        "outbox_id": int(outbox["id"]) if outbox else None,
                        "outbox_status": outbox["status"] if outbox else None,
                    }
                wallet = await (
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
                before = int(wallet["balance_tenths"]) if wallet else 0
                changed = before != 0
                if changed:
                    await conn.execute(
                        """
                    UPDATE wallets
                    SET balance_tenths = '0', updated_at = ?
                    WHERE owner_user_id = ?
                      AND customer_chat_id = ?
                    """,
                        (now, owner_user_id, customer_chat_id),
                    )
                    await conn.execute(
                        """
                    INSERT INTO ledger(
                        owner_user_id,
                        customer_chat_id,
                        source_message_id,
                        entry_type,
                        amount_tenths,
                        balance_before,
                        balance_after,
                        created_at_utc
                    )
                    VALUES (?, ?, ?, 'clear', ?, ?, '0', ?)
                    """,
                        (
                            owner_user_id,
                            customer_chat_id,
                            source_message_id,
                            str(abs(before)),
                            str(before),
                            now,
                        ),
                    )
                if changed:
                    (
                        deposit_total,
                        withdraw_total,
                        entry_count,
                    ) = await self.store._get_stats_for_update(
                        conn, owner_user_id, customer_chat_id
                    )
                    await self.store._write_stats(
                        conn,
                        owner_user_id,
                        customer_chat_id,
                        deposit_total,
                        withdraw_total,
                        entry_count + 1,
                        now,
                    )
                await conn.execute(
                    """
                INSERT INTO processed_messages(
                    owner_user_id,
                    customer_chat_id,
                    source_message_id,
                    command_type,
                    processed_at_utc
                )
                VALUES (?, ?, ?, 'clear', ?)
                """,
                    (owner_user_id, customer_chat_id, source_message_id, now),
                )
                receipt_text = "余额已清零"
                outbox_id = await self.store._insert_receipt_outbox(
                    conn,
                    owner_user_id=owner_user_id,
                    customer_chat_id=customer_chat_id,
                    source_message_id=source_message_id,
                    business_connection_id=business_connection_id,
                    receipt_type="clear",
                    receipt_text=receipt_text,
                    should_pin=changed,
                    now=now,
                )
                await conn.commit()
                return {
                    "duplicate": False,
                    "changed": changed,
                    "before": before,
                    "after": 0,
                    "receipt_text": receipt_text,
                    "outbox_id": outbox_id,
                }
            except BaseException:
                await conn.rollback()
                raise
