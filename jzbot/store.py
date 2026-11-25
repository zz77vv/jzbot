"""Atomic SQLite repository for JZBot."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Literal

from jzbot.money import MAX_SAFE_TENTHS, format_receipt, utc_now_iso
from jzbot.schema import connect_db


@dataclass(frozen=True)
class CustomerSummary:
    owner_id: int
    chat_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    has_wallet: bool
    balance_tenths: int
    deposit_total_tenths: int
    withdraw_total_tenths: int
    entry_count: int
    is_disabled: bool
    created_at: str
    updated_at: str

    @property
    def display_name(self) -> str:
        parts = [p for p in (self.first_name, self.last_name) if p]
        if parts:
            return " ".join(parts)
        if self.username:
            return f"@{self.username}"
        return f"ID:{self.chat_id}"


@dataclass(frozen=True)
class TransactionRow:
    id: int
    owner_id: int
    chat_id: int
    kind: str
    amount_tenths: int
    balance_before: int
    balance_after: int
    source_message_id: int | None
    operator_id: int
    created_at: str


@dataclass(frozen=True)
class ReceiptRow:
    id: int
    owner_id: int
    chat_id: int
    source_message_id: int
    connection_id: str
    receipt_type: str
    receipt_text: str
    reply_to_message_id: int | None
    should_pin: bool
    status: str
    attempt_count: int
    created_at: str
    next_attempt_at: str
    delivered_at: str | None
    last_error: str | None


@dataclass(frozen=True)
class BookkeepingResult:
    applied: bool
    duplicate: bool
    balance_before: int
    balance_after: int
    receipt_id: int | None
    receipt_text: str | None


@dataclass(frozen=True)
class OwnerOverview:
    total_customers: int
    positive_count: int
    positive_sum_tenths: int
    debt_count: int
    debt_sum_tenths: int
    zero_count: int
    pending_receipts: int
    alert_receipts: int


@dataclass(frozen=True)
class BroadcastJobRow:
    id: int
    owner_id: int
    scopes: list[str]
    message_text: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class BroadcastRecipientRow:
    id: int
    job_id: int
    chat_id: int
    status: str
    attempts: int
    next_attempt_at: str
    message_id: int | None
    error_reason: str | None
    updated_at: str


def _row_to_customer(row: sqlite3.Row) -> CustomerSummary:
    return CustomerSummary(
        owner_id=row["owner_id"],
        chat_id=row["chat_id"],
        username=row["username"],
        first_name=row["first_name"],
        last_name=row["last_name"],
        has_wallet=bool(row["has_wallet"]),
        balance_tenths=int(row["balance_tenths"]),
        deposit_total_tenths=int(row["deposit_total_tenths"]),
        withdraw_total_tenths=int(row["withdraw_total_tenths"]),
        entry_count=int(row["entry_count"]),
        is_disabled=bool(row["is_disabled"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_tx(row: sqlite3.Row) -> TransactionRow:
    return TransactionRow(
        id=row["id"],
        owner_id=row["owner_id"],
        chat_id=row["chat_id"],
        kind=row["kind"],
        amount_tenths=int(row["amount_tenths"]),
        balance_before=int(row["balance_before"]),
        balance_after=int(row["balance_after"]),
        source_message_id=row["source_message_id"],
        operator_id=row["operator_id"],
        created_at=row["created_at"],
    )


def _row_to_receipt(row: sqlite3.Row) -> ReceiptRow:
    return ReceiptRow(
        id=row["id"],
        owner_id=row["owner_id"],
        chat_id=row["chat_id"],
        source_message_id=row["source_message_id"],
        connection_id=row["connection_id"],
        receipt_type=row["receipt_type"],
        receipt_text=row["receipt_text"],
        reply_to_message_id=row["reply_to_message_id"],
        should_pin=bool(row["should_pin"]),
        status=row["status"],
        attempt_count=int(row["attempt_count"]),
        created_at=row["created_at"],
        next_attempt_at=row["next_attempt_at"],
        delivered_at=row["delivered_at"],
        last_error=row["last_error"],
    )


class Store:
    """Thread-safe, transactional SQLite store for JZBot."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = connect_db(db_path)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def integrity_check(self) -> tuple[bool, int]:
        with self._lock:
            qc = self._conn.execute("PRAGMA quick_check").fetchone()[0]
            fk_errors = len(self._conn.execute("PRAGMA foreign_key_check").fetchall())
            return (qc == "ok", fk_errors)

    def backup_to(self, dest_path: Path) -> Path:
        """Create a consistent hot SQLite backup using SQLite's online backup API."""
        import os

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            try:
                os.chmod(dest_path.parent, 0o700)
            except OSError:
                pass
        with self._lock:
            dst = sqlite3.connect(str(dest_path))
            try:
                self._conn.backup(dst)
            finally:
                dst.close()
        if os.name == "posix":
            try:
                os.chmod(dest_path, 0o600)
            except OSError:
                pass
        return dest_path

    def mark_owner_welcomed(self, owner_id: int) -> bool:
        """Mark owner as welcomed; returns True only on the first transition 0 -> 1."""
        now = utc_now_iso()
        with self._write_tx() as conn:
            cur = conn.execute(
                "UPDATE owners SET welcome_sent = 1, updated_at = ? WHERE owner_id = ? AND welcome_sent = 0",
                (now, owner_id),
            )
            return cur.rowcount > 0

    @contextmanager
    def _write_tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------
    # Owners & Business Connections
    # ------------------------------------------------------------------

    def upsert_connection(self, connection_id: str, owner_id: int, is_enabled: bool) -> None:
        now = utc_now_iso()
        with self._write_tx() as conn:
            conn.execute(
                """
                INSERT INTO owners(owner_id, welcome_sent, created_at, updated_at)
                VALUES (?, 0, ?, ?)
                ON CONFLICT(owner_id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (owner_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO connections(connection_id, owner_id, is_enabled, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(connection_id) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    is_enabled = excluded.is_enabled,
                    updated_at = excluded.updated_at
                """,
                (connection_id, owner_id, 1 if is_enabled else 0, now),
            )

    def get_connection(self, connection_id: str) -> tuple[int, bool] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT owner_id, is_enabled FROM connections WHERE connection_id = ?",
                (connection_id,),
            ).fetchone()
            if not row:
                return None
            return (int(row["owner_id"]), bool(row["is_enabled"]))

    def list_owner_connections(self, owner_id: int, limit: int = 8) -> list[tuple[str, bool]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT connection_id, is_enabled
                FROM connections
                WHERE owner_id = ?
                ORDER BY is_enabled DESC, updated_at DESC
                LIMIT ?
                """,
                (owner_id, limit),
            ).fetchall()
            return [(str(r["connection_id"]), bool(r["is_enabled"])) for r in rows]

    def is_known_owner(self, user_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM owners WHERE owner_id = ?",
                (user_id,),
            ).fetchone()
            return row is not None

    def upsert_customer_profile(
        self,
        owner_id: int,
        chat_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> None:
        now = utc_now_iso()
        with self._write_tx() as conn:
            conn.execute(
                """
                INSERT INTO owners(owner_id, welcome_sent, created_at, updated_at)
                VALUES (?, 0, ?, ?)
                ON CONFLICT(owner_id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (owner_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO customers(
                    owner_id, chat_id, username, first_name, last_name,
                    has_wallet, balance_tenths, deposit_total_tenths,
                    withdraw_total_tenths, entry_count, is_disabled,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0, 0, 0, ?, ?)
                ON CONFLICT(owner_id, chat_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name
                """,
                (owner_id, chat_id, username, first_name, last_name, now, now),
            )

    # ------------------------------------------------------------------
    # Atomic Bookkeeping (+ / - / //)
    # ------------------------------------------------------------------

    def record_amount(
        self,
        *,
        owner_id: int,
        chat_id: int,
        source_message_id: int,
        connection_id: str,
        kind: Literal["deposit", "withdraw"],
        amount_tenths: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> BookkeepingResult:
        if amount_tenths <= 0:
            raise ValueError("amount_tenths must be positive")
        now = utc_now_iso()
        with self._write_tx() as conn:
            dup = conn.execute(
                """
                SELECT 1 FROM processed_messages
                WHERE owner_id = ? AND chat_id = ? AND source_message_id = ?
                """,
                (owner_id, chat_id, source_message_id),
            ).fetchone()
            if dup:
                cur = conn.execute(
                    "SELECT balance_tenths FROM customers WHERE owner_id = ? AND chat_id = ?",
                    (owner_id, chat_id),
                ).fetchone()
                bal = int(cur["balance_tenths"]) if cur else 0
                return BookkeepingResult(False, True, bal, bal, None, None)

            conn.execute(
                """
                INSERT INTO owners(owner_id, welcome_sent, created_at, updated_at)
                VALUES (?, 0, ?, ?)
                ON CONFLICT(owner_id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (owner_id, now, now),
            )

            existing = conn.execute(
                "SELECT balance_tenths, deposit_total_tenths, withdraw_total_tenths, entry_count FROM customers WHERE owner_id = ? AND chat_id = ?",
                (owner_id, chat_id),
            ).fetchone()

            before = int(existing["balance_tenths"]) if existing else 0
            dep_total = int(existing["deposit_total_tenths"]) if existing else 0
            wd_total = int(existing["withdraw_total_tenths"]) if existing else 0
            entries = int(existing["entry_count"]) if existing else 0

            if kind == "deposit":
                after = before + amount_tenths
                dep_total += amount_tenths
            else:
                after = before - amount_tenths
                wd_total += amount_tenths
            if abs(after) > MAX_SAFE_TENTHS or dep_total > MAX_SAFE_TENTHS or wd_total > MAX_SAFE_TENTHS:
                raise OverflowError("Balance or cumulative total exceeds 64-bit safe range")
            entries += 1

            conn.execute(
                """
                INSERT INTO customers(
                    owner_id, chat_id, username, first_name, last_name,
                    has_wallet, balance_tenths, deposit_total_tenths,
                    withdraw_total_tenths, entry_count, is_disabled,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(owner_id, chat_id) DO UPDATE SET
                    username = COALESCE(excluded.username, customers.username),
                    first_name = COALESCE(excluded.first_name, customers.first_name),
                    last_name = COALESCE(excluded.last_name, customers.last_name),
                    has_wallet = 1,
                    balance_tenths = excluded.balance_tenths,
                    deposit_total_tenths = excluded.deposit_total_tenths,
                    withdraw_total_tenths = excluded.withdraw_total_tenths,
                    entry_count = excluded.entry_count,
                    updated_at = excluded.updated_at
                """,
                (owner_id, chat_id, username, first_name, last_name, after, dep_total, wd_total, entries, now, now),
            )

            conn.execute(
                """
                INSERT INTO transactions(
                    owner_id, chat_id, kind, amount_tenths,
                    balance_before, balance_after, source_message_id,
                    operator_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (owner_id, chat_id, kind, amount_tenths, before, after, source_message_id, owner_id, now),
            )

            conn.execute(
                """
                INSERT INTO processed_messages(owner_id, chat_id, source_message_id, command_type, processed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (owner_id, chat_id, source_message_id, kind, now),
            )

            receipt_text = format_receipt(kind, amount_tenths, after)
            cur = conn.execute(
                """
                INSERT INTO receipt_outbox(
                    owner_id, chat_id, source_message_id, connection_id,
                    receipt_type, receipt_text, reply_to_message_id, should_pin,
                    status, attempt_count, created_at, next_attempt_at
                )
                VALUES (?, ?, ?, ?, 'amount', ?, ?, 1, 'pending', 0, ?, ?)
                """,
                (owner_id, chat_id, source_message_id, connection_id, receipt_text, source_message_id, now, now),
            )
            return BookkeepingResult(True, False, before, after, int(cur.lastrowid), receipt_text)

    def record_clear(
        self,
        *,
        owner_id: int,
        chat_id: int,
        source_message_id: int,
        connection_id: str,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> BookkeepingResult:
        """Clear customer balance via `//`.

        If prior balance is non-zero: write clear transaction, zero wallet, pin receipt.
        If prior balance is zero or wallet doesn't exist: record processed_message & unpinned receipt,
        without creating an empty wallet or ledger entry.
        """
        now = utc_now_iso()
        with self._write_tx() as conn:
            dup = conn.execute(
                """
                SELECT 1 FROM processed_messages
                WHERE owner_id = ? AND chat_id = ? AND source_message_id = ?
                """,
                (owner_id, chat_id, source_message_id),
            ).fetchone()
            if dup:
                cur = conn.execute(
                    "SELECT balance_tenths FROM customers WHERE owner_id = ? AND chat_id = ?",
                    (owner_id, chat_id),
                ).fetchone()
                bal = int(cur["balance_tenths"]) if cur else 0
                return BookkeepingResult(False, True, bal, bal, None, None)

            conn.execute(
                """
                INSERT INTO owners(owner_id, welcome_sent, created_at, updated_at)
                VALUES (?, 0, ?, ?)
                ON CONFLICT(owner_id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (owner_id, now, now),
            )

            existing = conn.execute(
                "SELECT has_wallet, balance_tenths, entry_count FROM customers WHERE owner_id = ? AND chat_id = ?",
                (owner_id, chat_id),
            ).fetchone()
            before = int(existing["balance_tenths"]) if (existing and existing["has_wallet"]) else 0
            should_pin = 1 if before != 0 else 0

            if before != 0:
                entries = int(existing["entry_count"]) + 1
                conn.execute(
                    """
                    UPDATE customers
                    SET username = COALESCE(?, username),
                        first_name = COALESCE(?, first_name),
                        last_name = COALESCE(?, last_name),
                        balance_tenths = 0,
                        entry_count = ?,
                        updated_at = ?
                    WHERE owner_id = ? AND chat_id = ?
                    """,
                    (username, first_name, last_name, entries, now, owner_id, chat_id),
                )
                conn.execute(
                    """
                    INSERT INTO transactions(
                        owner_id, chat_id, kind, amount_tenths,
                        balance_before, balance_after, source_message_id,
                        operator_id, created_at
                    )
                    VALUES (?, ?, 'clear', ?, ?, 0, ?, ?, ?)
                    """,
                    (owner_id, chat_id, abs(before), before, source_message_id, owner_id, now),
                )

            conn.execute(
                """
                INSERT INTO processed_messages(owner_id, chat_id, source_message_id, command_type, processed_at)
                VALUES (?, ?, ?, 'clear', ?)
                """,
                (owner_id, chat_id, source_message_id, now),
            )

            receipt_text = format_receipt("clear", 0, 0)
            cur = conn.execute(
                """
                INSERT INTO receipt_outbox(
                    owner_id, chat_id, source_message_id, connection_id,
                    receipt_type, receipt_text, reply_to_message_id, should_pin,
                    status, attempt_count, created_at, next_attempt_at
                )
                VALUES (?, ?, ?, ?, 'clear', ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (owner_id, chat_id, source_message_id, connection_id, receipt_text, source_message_id, should_pin, now, now),
            )
            return BookkeepingResult(True, False, before, 0, int(cur.lastrowid), receipt_text)

    def admin_zero_customer(
        self,
        *,
        owner_id: int,
        chat_id: int,
        operator_id: int,
        expected_balance_tenths: int,
        expected_entry_count: int,
    ) -> Literal["applied", "noop", "stale", "missing"]:
        """Atomically zero a customer balance from the admin panel with optimistic concurrency check."""
        now = utc_now_iso()
        with self._write_tx() as conn:
            row = conn.execute(
                "SELECT has_wallet, balance_tenths, entry_count FROM customers WHERE owner_id = ? AND chat_id = ?",
                (owner_id, chat_id),
            ).fetchone()
            if not row or not row["has_wallet"]:
                return "missing"
            bal = int(row["balance_tenths"])
            cnt = int(row["entry_count"])
            if bal != expected_balance_tenths or cnt != expected_entry_count:
                return "stale"
            if bal == 0:
                return "noop"
            conn.execute(
                """
                UPDATE customers
                SET balance_tenths = 0,
                    entry_count = entry_count + 1,
                    updated_at = ?
                WHERE owner_id = ? AND chat_id = ?
                """,
                (now, owner_id, chat_id),
            )
            conn.execute(
                """
                INSERT INTO transactions(
                    owner_id, chat_id, kind, amount_tenths,
                    balance_before, balance_after, source_message_id,
                    operator_id, created_at
                )
                VALUES (?, ?, 'admin_clear', ?, ?, 0, NULL, ?, ?)
                """,
                (owner_id, chat_id, abs(bal), bal, operator_id, now),
            )
            return "applied"

    def set_customer_disabled(self, owner_id: int, chat_id: int, is_disabled: bool) -> bool:
        with self._write_tx() as conn:
            cur = conn.execute(
                """
                UPDATE customers
                SET is_disabled = ?
                WHERE owner_id = ? AND chat_id = ?
                """,
                (1 if is_disabled else 0, owner_id, chat_id),
            )
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Queries & Admin Views
    # ------------------------------------------------------------------

    def get_customer(self, owner_id: int, chat_id: int) -> CustomerSummary | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM customers WHERE owner_id = ? AND chat_id = ?",
                (owner_id, chat_id),
            ).fetchone()
            return _row_to_customer(row) if row else None

    def get_recent_transactions(self, owner_id: int, chat_id: int, limit: int = 5, offset: int = 0) -> tuple[list[TransactionRow], int]:
        with self._lock:
            total = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM transactions WHERE owner_id = ? AND chat_id = ?",
                    (owner_id, chat_id),
                ).fetchone()[0]
            )
            rows = self._conn.execute(
                """
                SELECT * FROM transactions
                WHERE owner_id = ? AND chat_id = ?
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (owner_id, chat_id, limit, offset),
            ).fetchall()
            return ([_row_to_tx(r) for r in rows], total)

    def get_owner_overview(self, owner_id: int) -> OwnerOverview:
        alert_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        with self._lock:
            stats = self._conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN balance_tenths > 0 THEN 1 ELSE 0 END) AS pos_cnt,
                    COALESCE(SUM(CASE WHEN balance_tenths > 0 THEN balance_tenths ELSE 0 END), 0) AS pos_sum,
                    SUM(CASE WHEN balance_tenths < 0 THEN 1 ELSE 0 END) AS debt_cnt,
                    COALESCE(SUM(CASE WHEN balance_tenths < 0 THEN balance_tenths ELSE 0 END), 0) AS debt_sum,
                    SUM(CASE WHEN balance_tenths = 0 THEN 1 ELSE 0 END) AS zero_cnt
                FROM customers
                WHERE owner_id = ? AND has_wallet = 1
                """,
                (owner_id,),
            ).fetchone()
            pending = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM receipt_outbox WHERE owner_id = ? AND status = 'pending'",
                    (owner_id,),
                ).fetchone()[0]
            )
            alert = int(
                self._conn.execute(
                    """
                    SELECT COUNT(*) FROM receipt_outbox
                    WHERE owner_id = ? AND status = 'pending'
                      AND (attempt_count >= 3 OR created_at <= ?)
                    """,
                    (owner_id, alert_cutoff),
                ).fetchone()[0]
            )
            return OwnerOverview(
                total_customers=int(stats["total"] or 0),
                positive_count=int(stats["pos_cnt"] or 0),
                positive_sum_tenths=int(stats["pos_sum"] or 0),
                debt_count=int(stats["debt_cnt"] or 0),
                debt_sum_tenths=int(stats["debt_sum"] or 0),
                zero_count=int(stats["zero_cnt"] or 0),
                pending_receipts=pending,
                alert_receipts=alert,
            )

    def list_customers(
        self,
        owner_id: int,
        mode: Literal["all", "positive", "debt", "deposited"],
        limit: int = 10,
        offset: int = 0,
    ) -> tuple[list[CustomerSummary], int]:
        where = "owner_id = ? AND has_wallet = 1"
        order = "updated_at DESC, chat_id ASC"
        if mode == "positive":
            where += " AND balance_tenths > 0"
            order = "balance_tenths DESC, updated_at DESC"
        elif mode == "debt":
            where += " AND balance_tenths < 0"
            order = "balance_tenths ASC, updated_at DESC"
        elif mode == "deposited":
            where += " AND deposit_total_tenths > 0"
            order = "deposit_total_tenths DESC, updated_at DESC"

        with self._lock:
            total = int(self._conn.execute(f"SELECT COUNT(*) FROM customers WHERE {where}", (owner_id,)).fetchone()[0])
            rows = self._conn.execute(
                f"SELECT * FROM customers WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                (owner_id, limit, offset),
            ).fetchall()
            return ([_row_to_customer(r) for r in rows], total)

    def search_customers(self, owner_id: int, query: str, limit: int = 10, offset: int = 0) -> tuple[list[CustomerSummary], int]:
        q = query.strip()
        if not q or q == "@" or len(q) > 64:
            return ([], 0)
        q_clean = q.lstrip("@").lower()
        like = f"%{q_clean}%"
        sql_where = """
            owner_id = ? AND has_wallet = 1 AND (
                CAST(chat_id AS TEXT) LIKE ?
                OR LOWER(COALESCE(username, '')) LIKE ?
                OR LOWER(COALESCE(first_name, '')) LIKE ?
                OR LOWER(COALESCE(last_name, '')) LIKE ?
                OR LOWER(TRIM(COALESCE(first_name, '') || ' ' || COALESCE(last_name, ''))) LIKE ?
            )
        """
        params = (owner_id, like, like, like, like, like)
        with self._lock:
            total = int(self._conn.execute(f"SELECT COUNT(*) FROM customers WHERE {sql_where}", params).fetchone()[0])
            rows = self._conn.execute(
                f"SELECT * FROM customers WHERE {sql_where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            return ([_row_to_customer(r) for r in rows], total)

    # ------------------------------------------------------------------
    # Receipt Outbox
    # ------------------------------------------------------------------

    def get_due_receipts(self, limit: int = 20) -> list[ReceiptRow]:
        now = utc_now_iso()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM receipt_outbox
                WHERE status = 'pending' AND next_attempt_at <= ?
                ORDER BY next_attempt_at ASC, id ASC
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            return [_row_to_receipt(r) for r in rows]

    def has_due_receipts(self) -> bool:
        now = utc_now_iso()
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM receipt_outbox WHERE status = 'pending' AND next_attempt_at <= ? LIMIT 1",
                (now,),
            ).fetchone()
            return row is not None

    def list_pending_receipts(self, owner_id: int, limit: int = 10, offset: int = 0) -> tuple[list[ReceiptRow], int]:
        with self._lock:
            total = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM receipt_outbox WHERE owner_id = ? AND status = 'pending'",
                    (owner_id,),
                ).fetchone()[0]
            )
            rows = self._conn.execute(
                """
                SELECT * FROM receipt_outbox
                WHERE owner_id = ? AND status = 'pending'
                ORDER BY id ASC
                LIMIT ? OFFSET ?
                """,
                (owner_id, limit, offset),
            ).fetchall()
            return ([_row_to_receipt(r) for r in rows], total)

    def mark_receipt_delivered(self, receipt_id: int) -> None:
        now = utc_now_iso()
        with self._write_tx() as conn:
            conn.execute(
                """
                UPDATE receipt_outbox
                SET status = 'delivered',
                    delivered_at = ?,
                    last_error = NULL
                WHERE id = ?
                """,
                (now, receipt_id),
            )

    def mark_receipt_retry(self, receipt_id: int, delay_seconds: float, error_text: str) -> None:
        next_at = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        with self._write_tx() as conn:
            conn.execute(
                """
                UPDATE receipt_outbox
                SET attempt_count = attempt_count + 1,
                    next_attempt_at = ?,
                    last_error = ?
                WHERE id = ?
                """,
                (next_at, error_text[:300], receipt_id),
            )

    def update_receipt_connection(self, receipt_id: int, connection_id: str) -> None:
        with self._write_tx() as conn:
            conn.execute(
                "UPDATE receipt_outbox SET connection_id = ? WHERE id = ? AND status = 'pending'",
                (connection_id, receipt_id),
            )

    def retry_all_pending_receipts(self, owner_id: int) -> int:
        now = utc_now_iso()
        with self._write_tx() as conn:
            cur = conn.execute(
                "UPDATE receipt_outbox SET next_attempt_at = ? WHERE owner_id = ? AND status = 'pending'",
                (now, owner_id),
            )
            return cur.rowcount

    def get_latest_broadcast(self, owner_id: int) -> BroadcastJobRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM broadcast_jobs WHERE owner_id = ? ORDER BY id DESC LIMIT 1",
                (owner_id,),
            ).fetchone()
            if not row:
                return None
            return BroadcastJobRow(
                id=row["id"],
                owner_id=row["owner_id"],
                scopes=json.loads(row["scopes_json"]),
                message_text=row["message_text"],
                status=row["status"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    # ------------------------------------------------------------------
    # Broadcast
    # ------------------------------------------------------------------

    def select_broadcast_recipients(self, owner_id: int, scopes: set[str]) -> list[CustomerSummary]:
        clauses: list[str] = []
        if "traded" in scopes:
            clauses.append("entry_count > 0")
        if "non_negative" in scopes:
            clauses.append("balance_tenths >= 0")
        if "debt" in scopes:
            clauses.append("balance_tenths < 0")
        if not clauses:
            return []
        where = f"owner_id = ? AND has_wallet = 1 AND ({' OR '.join(clauses)})"
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM customers WHERE {where} ORDER BY updated_at DESC, chat_id ASC LIMIT 1001",
                (owner_id,),
            ).fetchall()
            return [_row_to_customer(r) for r in rows]

    def get_active_broadcast(self, owner_id: int) -> BroadcastJobRow | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM broadcast_jobs
                WHERE owner_id = ? AND status IN ('running', 'paused')
                ORDER BY id DESC LIMIT 1
                """,
                (owner_id,),
            ).fetchone()
            if not row:
                return None
            return BroadcastJobRow(
                id=row["id"],
                owner_id=row["owner_id"],
                scopes=json.loads(row["scopes_json"]),
                message_text=row["message_text"],
                status=row["status"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    def create_broadcast_job(self, owner_id: int, scopes: list[str], message_text: str, chat_ids: list[int]) -> int:
        now = utc_now_iso()
        with self._write_tx() as conn:
            active = conn.execute(
                "SELECT id FROM broadcast_jobs WHERE owner_id = ? AND status IN ('running', 'paused')",
                (owner_id,),
            ).fetchone()
            if active:
                raise RuntimeError("Owner already has an active broadcast job")
            cur = conn.execute(
                """
                INSERT INTO broadcast_jobs(owner_id, scopes_json, message_text, status, created_at, updated_at)
                VALUES (?, ?, ?, 'running', ?, ?)
                """,
                (owner_id, json.dumps(sorted(scopes)), message_text, now, now),
            )
            job_id = int(cur.lastrowid)
            conn.executemany(
                """
                INSERT INTO broadcast_recipients(job_id, chat_id, status, attempts, next_attempt_at, updated_at)
                VALUES (?, ?, 'pending', 0, ?, ?)
                """,
                [(job_id, cid, now, now) for cid in chat_ids],
            )
            return job_id

    def recover_broadcasts_on_startup(self) -> None:
        """Pause running jobs and mark in-flight `sending` items as `uncertain` on startup."""
        now = utc_now_iso()
        with self._write_tx() as conn:
            conn.execute(
                """
                UPDATE broadcast_recipients
                SET status = 'uncertain',
                    error_reason = 'Process restarted while sending',
                    updated_at = ?
                WHERE status = 'sending'
                """,
                (now,),
            )
            conn.execute(
                """
                UPDATE broadcast_jobs
                SET status = 'paused', updated_at = ?
                WHERE status = 'running'
                """,
                (now,),
            )

    def set_broadcast_status(self, job_id: int, owner_id: int, status: Literal["running", "paused", "cancelled"]) -> bool:
        now = utc_now_iso()
        with self._write_tx() as conn:
            cur = conn.execute(
                "UPDATE broadcast_jobs SET status = ?, updated_at = ? WHERE id = ? AND owner_id = ? AND status IN ('running', 'paused')",
                (status, now, job_id, owner_id),
            )
            if cur.rowcount == 0:
                return False
            if status == "cancelled":
                conn.execute(
                    "UPDATE broadcast_recipients SET status = 'cancelled', updated_at = ? WHERE job_id = ? AND status = 'pending'",
                    (now, job_id),
                )
            return True

    def claim_next_broadcast_recipient(self) -> tuple[BroadcastJobRow, BroadcastRecipientRow] | None:
        now = utc_now_iso()
        with self._write_tx() as conn:
            row = conn.execute(
                """
                SELECT r.*, j.owner_id, j.scopes_json, j.message_text, j.status AS job_status,
                       j.created_at AS job_created_at, j.updated_at AS job_updated_at
                FROM broadcast_recipients r
                JOIN broadcast_jobs j ON j.id = r.job_id
                WHERE j.status = 'running' AND r.status = 'pending' AND r.next_attempt_at <= ?
                ORDER BY r.next_attempt_at ASC, r.id ASC
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if not row:
                # Auto-complete running jobs that have no pending/sending recipients left
                conn.execute(
                    """
                    UPDATE broadcast_jobs
                    SET status = 'completed', updated_at = ?
                    WHERE status = 'running'
                      AND NOT EXISTS (
                          SELECT 1 FROM broadcast_recipients r
                          WHERE r.job_id = broadcast_jobs.id AND r.status IN ('pending', 'sending')
                      )
                    """,
                    (now,),
                )
                return None
            conn.execute(
                "UPDATE broadcast_recipients SET status = 'sending', attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            job = BroadcastJobRow(
                id=row["job_id"],
                owner_id=row["owner_id"],
                scopes=json.loads(row["scopes_json"]),
                message_text=row["message_text"],
                status=row["job_status"],
                created_at=row["job_created_at"],
                updated_at=row["job_updated_at"],
            )
            rec = BroadcastRecipientRow(
                id=row["id"],
                job_id=row["job_id"],
                chat_id=row["chat_id"],
                status="sending",
                attempts=int(row["attempts"]) + 1,
                next_attempt_at=row["next_attempt_at"],
                message_id=row["message_id"],
                error_reason=row["error_reason"],
                updated_at=now,
            )
            return (job, rec)

    def finish_broadcast_recipient(
        self,
        recipient_id: int,
        status: Literal["sent", "failed", "uncertain", "pending"],
        *,
        message_id: int | None = None,
        error_reason: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        now = utc_now_iso()
        next_at = now
        if retry_after_seconds is not None:
            next_at = (datetime.now(timezone.utc) + timedelta(seconds=retry_after_seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        with self._write_tx() as conn:
            conn.execute(
                """
                UPDATE broadcast_recipients
                SET status = ?,
                    message_id = COALESCE(?, message_id),
                    error_reason = ?,
                    next_attempt_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, message_id, error_reason[:300] if error_reason else None, next_at, now, recipient_id),
            )

    def get_broadcast_counts(self, job_id: int) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM broadcast_recipients WHERE job_id = ? GROUP BY status",
                (job_id,),
            ).fetchall()
            counts = {"pending": 0, "sending": 0, "sent": 0, "failed": 0, "uncertain": 0, "cancelled": 0}
            for r in rows:
                counts[str(r["status"])] = int(r["cnt"])
            return counts
