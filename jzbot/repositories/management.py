from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from jzbot.domain import utc_now_iso
from jzbot.repositories.base import BaseRepository

logger = logging.getLogger("jzbot")


class ManagementRepository(BaseRepository):
    async def _admin_wallet_snapshot(self, owner: int, actor: int, customer: int):
        """Must be called inside the caller's BEGIN IMMEDIATE/write_lock."""
        if actor != owner:
            raise PermissionError("Business owner required")
        row = await (
            await self._require_conn().execute(
                """
        SELECT w.balance_tenths, COALESCE(s.entry_count, 0) AS entry_count,
            (SELECT COALESCE(MAX(id),0) FROM ledger
             WHERE owner_user_id=w.owner_user_id AND customer_chat_id=w.customer_chat_id) AS ledger_id,
            (SELECT COALESCE(MAX(id),0) FROM admin_operations
             WHERE owner_user_id=w.owner_user_id AND customer_chat_id=w.customer_chat_id
             AND result='applied') AS admin_id
        FROM wallets w JOIN business_accounts b ON b.owner_user_id=w.owner_user_id
        LEFT JOIN wallet_stats s ON s.owner_user_id=w.owner_user_id
            AND s.customer_chat_id=w.customer_chat_id
        WHERE w.owner_user_id=? AND w.customer_chat_id=?
    """,
                (owner, customer),
            )
        ).fetchone()
        if row is None:
            raise PermissionError("Customer is not in this owner's ledger")
        return row

    async def set_customer_disabled(
        self, owner: int, actor: int, customer: int, disabled: bool
    ) -> None:
        conn = self._require_conn()
        async with self.write_lock:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                await self._admin_wallet_snapshot(owner, actor, customer)
                await conn.execute(
                    """
                INSERT INTO customer_management VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(owner_user_id, customer_chat_id) DO UPDATE SET
                    is_disabled=excluded.is_disabled, updated_by=excluded.updated_by,
                    updated_at_utc=excluded.updated_at_utc
            """,
                    (owner, customer, int(disabled), actor, utc_now_iso()),
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def create_admin_zero(
        self,
        owner: int,
        actor: int,
        customer: int,
        back_kind: str = "a",
        back_page: int = 0,
        *,
        now: datetime | None = None,
    ):
        conn = self._require_conn()
        now = now or datetime.now(timezone.utc)
        operation = secrets.token_hex(12)
        async with self.write_lock:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                wallet = await self._admin_wallet_snapshot(owner, actor, customer)
                request = dict(
                    operation_id=operation,
                    owner_user_id=owner,
                    administrator_id=actor,
                    customer_chat_id=customer,
                    expected_balance=str(int(wallet["balance_tenths"])),
                    expected_ledger_id=wallet["ledger_id"],
                    expected_admin_id=wallet["admin_id"],
                    expected_entry_count=wallet["entry_count"],
                    created_at_utc=now.isoformat(),
                    expires_at_utc=(now + timedelta(minutes=5)).isoformat(),
                    back_kind=back_kind if back_kind in {"a", "p", "d", "s"} else "a",
                    back_page=max(0, min(int(back_page), 999999)),
                )
                await conn.rollback()
                return request
            except BaseException:
                await conn.rollback()
                raise

    async def _insert_admin_request(self, conn, request):
        keys = (
            "operation_id",
            "owner_user_id",
            "administrator_id",
            "customer_chat_id",
            "expected_balance",
            "expected_ledger_id",
            "expected_admin_id",
            "expected_entry_count",
            "created_at_utc",
            "expires_at_utc",
            "back_kind",
            "back_page",
        )
        await conn.execute(
            """
        INSERT INTO admin_operations(operation_id, owner_user_id, administrator_id,
            customer_chat_id, expected_balance, expected_ledger_id, expected_admin_id,
            expected_entry_count, created_at_utc, expires_at_utc, back_kind, back_page)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
            tuple((request[key] for key in keys)),
        )

    async def resolve_admin_zero(
        self, owner: int, actor: int, request: dict, *, now: datetime | None = None
    ):
        """Atomic confirmation, version check, balance/stats and audit clear event.

        The applied audit row IS the separate admin-origin clear ledger record.
        No customer-message ledger/processed_messages/Outbox writes occur here.
        """
        conn = self._require_conn()
        now = now or datetime.now(timezone.utc)
        operation = request["operation_id"]
        if (
            request["owner_user_id"] != owner
            or request["administrator_id"] != actor
            or (not re.fullmatch("[a-f0-9]{24}", operation))
        ):
            raise PermissionError("Untrusted confirmation")
        async with self.write_lock:
            await conn.execute("BEGIN IMMEDIATE")
            authorized = False
            try:
                row = await (
                    await conn.execute(
                        """
                SELECT * FROM admin_operations
                WHERE operation_id=?
            """,
                        (operation,),
                    )
                ).fetchone()
                wallet = await self._admin_wallet_snapshot(
                    owner, actor, request["customer_chat_id"]
                )
                if row is not None:
                    if (
                        row["owner_user_id"] != owner
                        or row["administrator_id"] != actor
                        or row["customer_chat_id"] != request["customer_chat_id"]
                    ):
                        raise PermissionError("Confirmation identity mismatch")
                    await conn.rollback()
                    return (dict(row), True)
                authorized = True
                row = request
                await self._insert_admin_request(conn, request)
                before = int(wallet["balance_tenths"])
                result = "applied"
                if now >= datetime.fromisoformat(row["expires_at_utc"]):
                    result = "expired"
                elif (
                    before != int(row["expected_balance"])
                    or wallet["ledger_id"] != row["expected_ledger_id"]
                    or wallet["admin_id"] != row["expected_admin_id"]
                    or (wallet["entry_count"] != row["expected_entry_count"])
                ):
                    result = "stale"
                elif before == 0:
                    result = "noop"
                anchor = None
                after = before
                if result == "applied":
                    anchor = (
                        await (
                            await conn.execute(
                                "SELECT COALESCE(MAX(id),0) AS id FROM ledger"
                            )
                        ).fetchone()
                    )["id"]
                    await conn.execute(
                        """
                    UPDATE wallets SET balance_tenths='0', updated_at=?
                    WHERE owner_user_id=? AND customer_chat_id=?
                """,
                        (now.isoformat(), owner, row["customer_chat_id"]),
                    )
                    (
                        deposits,
                        withdrawals,
                        count,
                    ) = await self.store._get_stats_for_update(
                        conn, owner, row["customer_chat_id"]
                    )
                    await self.store._write_stats(
                        conn,
                        owner,
                        row["customer_chat_id"],
                        deposits,
                        withdrawals,
                        count + 1,
                        now.isoformat(),
                    )
                    after = 0
                await conn.execute(
                    """
                UPDATE admin_operations SET result=?, balance_before=?, balance_after=?,
                    completed_at_utc=?, ledger_anchor_id=?
                WHERE operation_id=? AND result='pending'
            """,
                    (
                        result,
                        str(before),
                        str(after),
                        now.isoformat(),
                        anchor,
                        operation,
                    ),
                )
                updated = await (
                    await conn.execute(
                        "SELECT * FROM admin_operations WHERE operation_id=?",
                        (operation,),
                    )
                ).fetchone()
                await conn.commit()
                logger.info(
                    "Admin zero | owner=%s administrator=%s customer=%s operation=%s result=%s",
                    owner,
                    actor,
                    row["customer_chat_id"],
                    operation,
                    result,
                )
                return (dict(updated), False)
            except BaseException as exc:
                await conn.rollback()
                if authorized and isinstance(exc, Exception):
                    try:
                        await conn.execute("BEGIN IMMEDIATE")
                        await self._insert_admin_request(conn, request)
                        current = await self._admin_wallet_snapshot(
                            owner, actor, request["customer_chat_id"]
                        )
                        await conn.execute(
                            """
                        UPDATE admin_operations SET result='failed', balance_before=?,
                            balance_after=?, completed_at_utc=? WHERE operation_id=?
                    """,
                            (
                                str(int(current["balance_tenths"])),
                                str(int(current["balance_tenths"])),
                                now.isoformat(),
                                operation,
                            ),
                        )
                        await conn.commit()
                    except Exception:
                        await conn.rollback()
                        logger.exception(
                            "Admin zero failure audit unavailable | operation=%s",
                            operation,
                        )
                logger.warning(
                    "Admin zero not committed | owner=%s administrator=%s operation=%s",
                    owner,
                    actor,
                    operation,
                )
                raise
