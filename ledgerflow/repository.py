"""Transactional SQLite repository for LedgerFlow."""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Literal

from ledgerflow.database import open_database
from ledgerflow.domain import (
    MAX_SAFE_TENTHS,
    build_receipt_html,
    period_start_utc,
    utc_now_iso,
)


@dataclass(frozen=True)
class Contact:
    owner_id: int
    chat_id: int
    alias: str | None
    tags: list[str]
    tg_username: str | None
    tg_first_name: str | None
    tg_last_name: str | None
    has_ledger: bool
    balance_tenths: int
    total_in_tenths: int
    total_out_tenths: int
    tx_count: int
    archived: bool
    created_at: str
    updated_at: str

    @property
    def tg_display_name(self) -> str:
        parts = [p for p in (self.tg_first_name, self.tg_last_name) if p]
        if parts:
            return " ".join(parts)
        if self.tg_username:
            return f"@{self.tg_username}"
        return f"ID:{self.chat_id}"

    @property
    def label(self) -> str:
        if self.alias:
            return self.alias
        return self.tg_display_name


@dataclass(frozen=True)
class JournalEntry:
    id: int
    owner_id: int
    chat_id: int
    kind: str
    delta_tenths: int
    amount_tenths: int
    balance_before: int
    balance_after: int
    note: str
    ref_entry_id: int | None
    voided_by_id: int | None
    tg_msg_id: int | None
    actor_id: int
    created_at: str


@dataclass(frozen=True)
class ReceiptItem:
    id: int
    owner_id: int
    chat_id: int
    tg_msg_id: int
    link_id: str
    html_text: str
    reply_msg_id: int | None
    should_pin: bool
    status: str
    attempts: int
    next_try_at: str
    created_at: str
    sent_at: str | None
    last_error: str | None


@dataclass(frozen=True)
class PostResult:
    applied: bool
    duplicate: bool
    journal_id: int | None
    balance_before: int
    balance_after: int
    receipt_id: int | None
    reason: str = ""


@dataclass(frozen=True)
class DashboardStats:
    total_contacts: int
    positive_count: int
    positive_sum: int
    debt_count: int
    debt_sum: int
    zero_count: int
    today_in: int
    today_out: int
    today_tx_count: int
    pending_receipts: int
    auto_pin: bool


@dataclass(frozen=True)
class FinancialReport:
    period: str
    total_in: int
    total_out: int
    net_flow: int
    tx_count: int
    active_contacts: int
    top_positive: list[Contact]
    top_debt: list[Contact]


@dataclass(frozen=True)
class AnnouncementJob:
    id: int
    owner_id: int
    target_desc: str
    body_text: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AnnouncementDelivery:
    id: int
    ann_id: int
    chat_id: int
    status: str
    attempts: int
    next_try_at: str
    tg_sent_msg_id: int | None
    error_text: str | None
    updated_at: str


def _parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def _serialize_tags(tags: list[str]) -> str:
    seen: list[str] = []
    for t in tags:
        clean = t.strip().replace(",", "")[:20]
        if clean and clean not in seen:
            seen.append(clean)
    return ",".join(seen[:8])


def _row_contact(r: sqlite3.Row) -> Contact:
    return Contact(
        owner_id=int(r["owner_id"]),
        chat_id=int(r["chat_id"]),
        alias=r["alias"],
        tags=_parse_tags(r["tags"]),
        tg_username=r["tg_username"],
        tg_first_name=r["tg_first_name"],
        tg_last_name=r["tg_last_name"],
        has_ledger=bool(r["has_ledger"]),
        balance_tenths=int(r["balance_tenths"]),
        total_in_tenths=int(r["total_in_tenths"]),
        total_out_tenths=int(r["total_out_tenths"]),
        tx_count=int(r["tx_count"]),
        archived=bool(r["archived"]),
        created_at=str(r["created_at"]),
        updated_at=str(r["updated_at"]),
    )


def _row_journal(r: sqlite3.Row) -> JournalEntry:
    return JournalEntry(
        id=int(r["id"]),
        owner_id=int(r["owner_id"]),
        chat_id=int(r["chat_id"]),
        kind=str(r["kind"]),
        delta_tenths=int(r["delta_tenths"]),
        amount_tenths=int(r["amount_tenths"]),
        balance_before=int(r["balance_before"]),
        balance_after=int(r["balance_after"]),
        note=str(r["note"]),
        ref_entry_id=int(r["ref_entry_id"]) if r["ref_entry_id"] is not None else None,
        voided_by_id=int(r["voided_by_id"]) if r["voided_by_id"] is not None else None,
        tg_msg_id=int(r["tg_msg_id"]) if r["tg_msg_id"] is not None else None,
        actor_id=int(r["actor_id"]),
        created_at=str(r["created_at"]),
    )


def _row_receipt(r: sqlite3.Row) -> ReceiptItem:
    return ReceiptItem(
        id=int(r["id"]),
        owner_id=int(r["owner_id"]),
        chat_id=int(r["chat_id"]),
        tg_msg_id=int(r["tg_msg_id"]),
        link_id=str(r["link_id"]),
        html_text=str(r["html_text"]),
        reply_msg_id=int(r["reply_msg_id"]) if r["reply_msg_id"] is not None else None,
        should_pin=bool(r["should_pin"]),
        status=str(r["status"]),
        attempts=int(r["attempts"]),
        next_try_at=str(r["next_try_at"]),
        created_at=str(r["created_at"]),
        sent_at=r["sent_at"],
        last_error=r["last_error"],
    )


class LedgerRepository:
    """Thread-safe atomic SQLite repository powering LedgerFlow."""

    def __init__(self, db_path: Path, currency_unit: str = "USDT") -> None:
        self.db_path = db_path
        self.currency_unit = currency_unit
        self._lock = threading.RLock()
        self._conn = open_database(db_path)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def health_check(self) -> tuple[bool, int]:
        with self._lock:
            qc = self._conn.execute("PRAGMA quick_check").fetchone()[0]
            fk = len(self._conn.execute("PRAGMA foreign_key_check").fetchall())
            return (qc == "ok", fk)

    def hot_backup(self, dest_path: Path) -> Path:
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

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
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
    # Accounts & Business Links
    # ------------------------------------------------------------------

    def upsert_link(self, link_id: str, owner_id: int, enabled: bool) -> None:
        now = utc_now_iso()
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO accounts(owner_id, welcomed, auto_pin, created_at, updated_at)
                VALUES (?, 0, 1, ?, ?)
                ON CONFLICT(owner_id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (owner_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO bus_links(link_id, owner_id, enabled, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(link_id) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (link_id, owner_id, 1 if enabled else 0, now),
            )

    def mark_welcomed(self, owner_id: int) -> bool:
        now = utc_now_iso()
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE accounts SET welcomed = 1, updated_at = ? WHERE owner_id = ? AND welcomed = 0",
                (now, owner_id),
            )
            return cur.rowcount > 0

    def toggle_auto_pin(self, owner_id: int) -> bool:
        now = utc_now_iso()
        with self._tx() as conn:
            row = conn.execute("SELECT auto_pin FROM accounts WHERE owner_id = ?", (owner_id,)).fetchone()
            new_val = 0 if (row and int(row["auto_pin"]) == 1) else 1
            conn.execute("UPDATE accounts SET auto_pin = ?, updated_at = ? WHERE owner_id = ?", (new_val, now, owner_id))
            return bool(new_val)

    def get_link(self, link_id: str) -> tuple[int, bool] | None:
        with self._lock:
            r = self._conn.execute("SELECT owner_id, enabled FROM bus_links WHERE link_id = ?", (link_id,)).fetchone()
            return (int(r["owner_id"]), bool(r["enabled"])) if r else None

    def list_owner_links(self, owner_id: int, limit: int = 8) -> list[tuple[str, bool]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT link_id, enabled FROM bus_links WHERE owner_id = ? ORDER BY enabled DESC, updated_at DESC LIMIT ?",
                (owner_id, limit),
            ).fetchall()
            return [(str(r["link_id"]), bool(r["enabled"])) for r in rows]

    def is_owner(self, user_id: int) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM accounts WHERE owner_id = ?", (user_id,)).fetchone() is not None

    def sync_contact_profile(
        self,
        owner_id: int,
        chat_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> None:
        now = utc_now_iso()
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO accounts(owner_id, welcomed, auto_pin, created_at, updated_at)
                VALUES (?, 0, 1, ?, ?)
                ON CONFLICT(owner_id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (owner_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO contacts(
                    owner_id, chat_id, tg_username, tg_first_name, tg_last_name,
                    has_ledger, balance_tenths, total_in_tenths, total_out_tenths,
                    tx_count, archived, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0, 0, 0, ?, ?)
                ON CONFLICT(owner_id, chat_id) DO UPDATE SET
                    tg_username = excluded.tg_username,
                    tg_first_name = excluded.tg_first_name,
                    tg_last_name = excluded.tg_last_name
                """,
                (owner_id, chat_id, username, first_name, last_name, now, now),
            )

    # ------------------------------------------------------------------
    # Core Ledger Mutations (in / out / void / settle / admin_adjust)
    # ------------------------------------------------------------------

    def post_entry(
        self,
        *,
        owner_id: int,
        chat_id: int,
        tg_msg_id: int,
        link_id: str,
        kind: Literal["in", "out"],
        amount_tenths: int,
        note: str = "",
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> PostResult:
        if amount_tenths <= 0:
            raise ValueError("amount_tenths must be positive")
        now = utc_now_iso()
        with self._tx() as conn:
            if conn.execute(
                "SELECT 1 FROM handled_msgs WHERE owner_id = ? AND chat_id = ? AND tg_msg_id = ?",
                (owner_id, chat_id, tg_msg_id),
            ).fetchone():
                row = conn.execute("SELECT balance_tenths FROM contacts WHERE owner_id = ? AND chat_id = ?", (owner_id, chat_id)).fetchone()
                bal = int(row["balance_tenths"]) if row else 0
                return PostResult(False, True, None, bal, bal, None, "duplicate")

            conn.execute(
                """
                INSERT INTO accounts(owner_id, welcomed, auto_pin, created_at, updated_at)
                VALUES (?, 0, 1, ?, ?)
                ON CONFLICT(owner_id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (owner_id, now, now),
            )
            acc = conn.execute("SELECT auto_pin FROM accounts WHERE owner_id = ?", (owner_id,)).fetchone()
            should_pin = int(acc["auto_pin"]) if acc else 1

            c = conn.execute(
                "SELECT balance_tenths, total_in_tenths, total_out_tenths, tx_count FROM contacts WHERE owner_id = ? AND chat_id = ?",
                (owner_id, chat_id),
            ).fetchone()
            before = int(c["balance_tenths"]) if c else 0
            tin = int(c["total_in_tenths"]) if c else 0
            tout = int(c["total_out_tenths"]) if c else 0
            cnt = int(c["tx_count"]) if c else 0

            delta = amount_tenths if kind == "in" else -amount_tenths
            after = before + delta
            if kind == "in":
                tin += amount_tenths
            else:
                tout += amount_tenths
            if abs(after) > MAX_SAFE_TENTHS or tin > MAX_SAFE_TENTHS or tout > MAX_SAFE_TENTHS:
                raise OverflowError("Ledger value exceeds safe INT64 bounds")

            conn.execute(
                """
                INSERT INTO contacts(
                    owner_id, chat_id, tg_username, tg_first_name, tg_last_name,
                    has_ledger, balance_tenths, total_in_tenths, total_out_tenths,
                    tx_count, archived, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(owner_id, chat_id) DO UPDATE SET
                    tg_username = COALESCE(excluded.tg_username, contacts.tg_username),
                    tg_first_name = COALESCE(excluded.tg_first_name, contacts.tg_first_name),
                    tg_last_name = COALESCE(excluded.tg_last_name, contacts.tg_last_name),
                    has_ledger = 1,
                    balance_tenths = excluded.balance_tenths,
                    total_in_tenths = excluded.total_in_tenths,
                    total_out_tenths = excluded.total_out_tenths,
                    tx_count = excluded.tx_count,
                    updated_at = excluded.updated_at
                """,
                (owner_id, chat_id, username, first_name, last_name, after, tin, tout, cnt + 1, now, now),
            )

            jcur = conn.execute(
                """
                INSERT INTO journal(
                    owner_id, chat_id, kind, delta_tenths, amount_tenths,
                    balance_before, balance_after, note, tg_msg_id, actor_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (owner_id, chat_id, kind, delta, amount_tenths, before, after, note, tg_msg_id, owner_id, now),
            )
            jid = int(jcur.lastrowid)

            conn.execute(
                "INSERT INTO handled_msgs(owner_id, chat_id, tg_msg_id, intent, handled_at) VALUES (?, ?, ?, ?, ?)",
                (owner_id, chat_id, tg_msg_id, kind, now),
            )

            html_text = build_receipt_html(
                kind=kind,
                amount_tenths=amount_tenths,
                balance_after=after,
                journal_id=jid,
                note=note,
                unit=self.currency_unit,
            )
            rcur = conn.execute(
                """
                INSERT INTO receipts(
                    owner_id, chat_id, tg_msg_id, link_id, html_text,
                    reply_msg_id, should_pin, status, attempts, next_try_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (owner_id, chat_id, tg_msg_id, link_id, html_text, tg_msg_id, should_pin, now, now),
            )
            return PostResult(True, False, jid, before, after, int(rcur.lastrowid))

    def void_last_entry(
        self,
        *,
        owner_id: int,
        chat_id: int,
        actor_id: int,
        tg_msg_id: int | None = None,
        link_id: str | None = None,
    ) -> PostResult:
        """Reverse the most recent non-voided `in`, `out`, or `adjust` entry for this contact."""
        now = utc_now_iso()
        with self._tx() as conn:
            if tg_msg_id is not None and conn.execute(
                "SELECT 1 FROM handled_msgs WHERE owner_id = ? AND chat_id = ? AND tg_msg_id = ?",
                (owner_id, chat_id, tg_msg_id),
            ).fetchone():
                row = conn.execute("SELECT balance_tenths FROM contacts WHERE owner_id = ? AND chat_id = ?", (owner_id, chat_id)).fetchone()
                bal = int(row["balance_tenths"]) if row else 0
                return PostResult(False, True, None, bal, bal, None, "duplicate")

            target = conn.execute(
                """
                SELECT * FROM journal
                WHERE owner_id = ? AND chat_id = ?
                  AND kind IN ('in', 'out', 'adjust')
                  AND voided_by_id IS NULL
                ORDER BY id DESC LIMIT 1
                """,
                (owner_id, chat_id),
            ).fetchone()
            c = conn.execute(
                "SELECT balance_tenths, total_in_tenths, total_out_tenths, tx_count FROM contacts WHERE owner_id = ? AND chat_id = ?",
                (owner_id, chat_id),
            ).fetchone()
            if not target or not c:
                if tg_msg_id is not None:
                    conn.execute(
                        "INSERT INTO handled_msgs(owner_id, chat_id, tg_msg_id, intent, handled_at) VALUES (?, ?, ?, 'undo', ?)",
                        (owner_id, chat_id, tg_msg_id, now),
                    )
                bal = int(c["balance_tenths"]) if c else 0
                return PostResult(False, False, None, bal, bal, None, "nothing_to_void")

            before = int(c["balance_tenths"])
            rev_delta = -int(target["delta_tenths"])
            after = before + rev_delta
            tin = int(c["total_in_tenths"])
            tout = int(c["total_out_tenths"])
            if target["kind"] == "in":
                tin = max(0, tin - int(target["amount_tenths"]))
            elif target["kind"] == "out":
                tout = max(0, tout - int(target["amount_tenths"]))

            note = f"撤销 #{target['id']}"
            jcur = conn.execute(
                """
                INSERT INTO journal(
                    owner_id, chat_id, kind, delta_tenths, amount_tenths,
                    balance_before, balance_after, note, ref_entry_id, tg_msg_id, actor_id, created_at
                )
                VALUES (?, ?, 'void', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (owner_id, chat_id, rev_delta, abs(rev_delta), before, after, note, int(target["id"]), tg_msg_id, actor_id, now),
            )
            void_id = int(jcur.lastrowid)

            conn.execute("UPDATE journal SET voided_by_id = ? WHERE id = ?", (void_id, int(target["id"])))
            conn.execute(
                """
                UPDATE contacts
                SET balance_tenths = ?, total_in_tenths = ?, total_out_tenths = ?,
                    tx_count = tx_count + 1, updated_at = ?
                WHERE owner_id = ? AND chat_id = ?
                """,
                (after, tin, tout, now, owner_id, chat_id),
            )
            conn.execute(
                "INSERT INTO audit_log(owner_id, action, target_chat_id, detail, created_at) VALUES (?, 'void', ?, ?, ?)",
                (owner_id, chat_id, f"voided #{target['id']} by #{void_id}", now),
            )

            receipt_id: int | None = None
            if tg_msg_id is not None and link_id is not None:
                conn.execute(
                    "INSERT INTO handled_msgs(owner_id, chat_id, tg_msg_id, intent, handled_at) VALUES (?, ?, ?, 'undo', ?)",
                    (owner_id, chat_id, tg_msg_id, now),
                )
                acc = conn.execute("SELECT auto_pin FROM accounts WHERE owner_id = ?", (owner_id,)).fetchone()
                should_pin = int(acc["auto_pin"]) if acc else 1
                html_text = build_receipt_html(
                    kind="void",
                    amount_tenths=abs(rev_delta),
                    balance_after=after,
                    journal_id=void_id,
                    note=note,
                    unit=self.currency_unit,
                )
                rcur = conn.execute(
                    """
                    INSERT INTO receipts(
                        owner_id, chat_id, tg_msg_id, link_id, html_text,
                        reply_msg_id, should_pin, status, attempts, next_try_at, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                    """,
                    (owner_id, chat_id, tg_msg_id, link_id, html_text, tg_msg_id, should_pin, now, now),
                )
                receipt_id = int(rcur.lastrowid)

            return PostResult(True, False, void_id, before, after, receipt_id)

    def settle_balance(
        self,
        *,
        owner_id: int,
        chat_id: int,
        actor_id: int,
        tg_msg_id: int | None = None,
        link_id: str | None = None,
        expected_balance: int | None = None,
        expected_tx_count: int | None = None,
        note: str = "",
    ) -> PostResult:
        now = utc_now_iso()
        with self._tx() as conn:
            if tg_msg_id is not None and conn.execute(
                "SELECT 1 FROM handled_msgs WHERE owner_id = ? AND chat_id = ? AND tg_msg_id = ?",
                (owner_id, chat_id, tg_msg_id),
            ).fetchone():
                row = conn.execute("SELECT balance_tenths FROM contacts WHERE owner_id = ? AND chat_id = ?", (owner_id, chat_id)).fetchone()
                bal = int(row["balance_tenths"]) if row else 0
                return PostResult(False, True, None, bal, bal, None, "duplicate")

            c = conn.execute(
                "SELECT has_ledger, balance_tenths, tx_count FROM contacts WHERE owner_id = ? AND chat_id = ?",
                (owner_id, chat_id),
            ).fetchone()
            before = int(c["balance_tenths"]) if (c and c["has_ledger"]) else 0
            cnt = int(c["tx_count"]) if (c and c["has_ledger"]) else 0

            if expected_balance is not None and (not c or not c["has_ledger"] or before != expected_balance or cnt != expected_tx_count):
                return PostResult(False, False, None, before, before, None, "stale")

            jid: int | None = None
            if before != 0:
                jcur = conn.execute(
                    """
                    INSERT INTO journal(
                        owner_id, chat_id, kind, delta_tenths, amount_tenths,
                        balance_before, balance_after, note, tg_msg_id, actor_id, created_at
                    )
                    VALUES (?, ?, 'settle', ?, ?, ?, 0, ?, ?, ?, ?)
                    """,
                    (owner_id, chat_id, -before, abs(before), before, note or ("后台结清" if tg_msg_id is None else "清账"), tg_msg_id, actor_id, now),
                )
                jid = int(jcur.lastrowid)
                conn.execute(
                    "UPDATE contacts SET balance_tenths = 0, tx_count = tx_count + 1, updated_at = ? WHERE owner_id = ? AND chat_id = ?",
                    (now, owner_id, chat_id),
                )
                conn.execute(
                    "INSERT INTO audit_log(owner_id, action, target_chat_id, detail, created_at) VALUES (?, 'settle', ?, ?, ?)",
                    (owner_id, chat_id, f"settled {before} -> 0 (#{jid})", now),
                )

            receipt_id: int | None = None
            if tg_msg_id is not None and link_id is not None:
                conn.execute(
                    "INSERT INTO handled_msgs(owner_id, chat_id, tg_msg_id, intent, handled_at) VALUES (?, ?, ?, 'settle', ?)",
                    (owner_id, chat_id, tg_msg_id, now),
                )
                acc = conn.execute("SELECT auto_pin FROM accounts WHERE owner_id = ?", (owner_id,)).fetchone()
                should_pin = (1 if (acc and int(acc["auto_pin"]) == 1) else 0) if before != 0 else 0
                html_text = build_receipt_html(
                    kind="settle",
                    amount_tenths=abs(before),
                    balance_after=0,
                    journal_id=jid,
                    unit=self.currency_unit,
                )
                rcur = conn.execute(
                    """
                    INSERT INTO receipts(
                        owner_id, chat_id, tg_msg_id, link_id, html_text,
                        reply_msg_id, should_pin, status, attempts, next_try_at, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                    """,
                    (owner_id, chat_id, tg_msg_id, link_id, html_text, tg_msg_id, should_pin, now, now),
                )
                receipt_id = int(rcur.lastrowid)
                return PostResult(True, False, jid, before, 0, receipt_id)

            return PostResult(before != 0, False, jid, before, 0, None, "noop" if before == 0 else "")

    def admin_adjust(self, *, owner_id: int, chat_id: int, delta_tenths: int, note: str) -> PostResult:
        if delta_tenths == 0:
            raise ValueError("delta_tenths cannot be zero")
        now = utc_now_iso()
        with self._tx() as conn:
            c = conn.execute(
                "SELECT balance_tenths, total_in_tenths, total_out_tenths, tx_count FROM contacts WHERE owner_id = ? AND chat_id = ?",
                (owner_id, chat_id),
            ).fetchone()
            if not c:
                return PostResult(False, False, None, 0, 0, None, "missing")
            before = int(c["balance_tenths"])
            after = before + delta_tenths
            tin = int(c["total_in_tenths"]) + (delta_tenths if delta_tenths > 0 else 0)
            tout = int(c["total_out_tenths"]) + (-delta_tenths if delta_tenths < 0 else 0)
            if abs(after) > MAX_SAFE_TENTHS or tin > MAX_SAFE_TENTHS or tout > MAX_SAFE_TENTHS:
                raise OverflowError("Adjustment exceeds safe range")

            jcur = conn.execute(
                """
                INSERT INTO journal(
                    owner_id, chat_id, kind, delta_tenths, amount_tenths,
                    balance_before, balance_after, note, tg_msg_id, actor_id, created_at
                )
                VALUES (?, ?, 'adjust', ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (owner_id, chat_id, delta_tenths, abs(delta_tenths), before, after, note or "后台调账", owner_id, now),
            )
            jid = int(jcur.lastrowid)
            conn.execute(
                """
                UPDATE contacts
                SET has_ledger = 1, balance_tenths = ?, total_in_tenths = ?, total_out_tenths = ?,
                    tx_count = tx_count + 1, updated_at = ?
                WHERE owner_id = ? AND chat_id = ?
                """,
                (after, tin, tout, now, owner_id, chat_id),
            )
            conn.execute(
                "INSERT INTO audit_log(owner_id, action, target_chat_id, detail, created_at) VALUES (?, 'adjust', ?, ?, ?)",
                (owner_id, chat_id, f"delta={delta_tenths} note={note}", now),
            )
            return PostResult(True, False, jid, before, after, None)

    # ------------------------------------------------------------------
    # Contact Metadata (Alias, Tags, Archive)
    # ------------------------------------------------------------------

    def set_alias(self, owner_id: int, chat_id: int, alias: str | None) -> bool:
        clean = alias.strip()[:40] if alias and alias.strip() else None
        now = utc_now_iso()
        with self._tx() as conn:
            cur = conn.execute("UPDATE contacts SET alias = ? WHERE owner_id = ? AND chat_id = ?", (clean, owner_id, chat_id))
            if cur.rowcount:
                conn.execute(
                    "INSERT INTO audit_log(owner_id, action, target_chat_id, detail, created_at) VALUES (?, 'alias', ?, ?, ?)",
                    (owner_id, chat_id, clean or "<cleared>", now),
                )
            return cur.rowcount > 0

    def set_tags(self, owner_id: int, chat_id: int, tags: list[str]) -> bool:
        ser = _serialize_tags(tags)
        now = utc_now_iso()
        with self._tx() as conn:
            cur = conn.execute("UPDATE contacts SET tags = ? WHERE owner_id = ? AND chat_id = ?", (ser, owner_id, chat_id))
            if cur.rowcount:
                conn.execute(
                    "INSERT INTO audit_log(owner_id, action, target_chat_id, detail, created_at) VALUES (?, 'tags', ?, ?, ?)",
                    (owner_id, chat_id, ser, now),
                )
            return cur.rowcount > 0

    def set_archived(self, owner_id: int, chat_id: int, archived: bool) -> bool:
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE contacts SET archived = ? WHERE owner_id = ? AND chat_id = ?",
                (1 if archived else 0, owner_id, chat_id),
            )
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Queries, Reports & Exports
    # ------------------------------------------------------------------

    def get_contact(self, owner_id: int, chat_id: int) -> Contact | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM contacts WHERE owner_id = ? AND chat_id = ?", (owner_id, chat_id)).fetchone()
            return _row_contact(r) if r else None

    def get_journal(self, owner_id: int, chat_id: int, limit: int = 8, offset: int = 0) -> tuple[list[JournalEntry], int]:
        with self._lock:
            total = int(
                self._conn.execute("SELECT COUNT(*) FROM journal WHERE owner_id = ? AND chat_id = ?", (owner_id, chat_id)).fetchone()[0]
            )
            rows = self._conn.execute(
                "SELECT * FROM journal WHERE owner_id = ? AND chat_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                (owner_id, chat_id, limit, offset),
            ).fetchall()
            return ([_row_journal(r) for r in rows], total)

    def get_all_journal_for_export(self, owner_id: int, chat_id: int | None = None) -> list[tuple[JournalEntry, str]]:
        with self._lock:
            if chat_id is not None:
                rows = self._conn.execute(
                    """
                    SELECT j.*, COALESCE(c.alias, TRIM(COALESCE(c.tg_first_name,'') || ' ' || COALESCE(c.tg_last_name,'')), c.tg_username, CAST(c.chat_id AS TEXT)) AS c_label
                    FROM journal j
                    JOIN contacts c ON c.owner_id = j.owner_id AND c.chat_id = j.chat_id
                    WHERE j.owner_id = ? AND j.chat_id = ?
                    ORDER BY j.id DESC
                    """,
                    (owner_id, chat_id),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT j.*, COALESCE(c.alias, TRIM(COALESCE(c.tg_first_name,'') || ' ' || COALESCE(c.tg_last_name,'')), c.tg_username, CAST(c.chat_id AS TEXT)) AS c_label
                    FROM journal j
                    JOIN contacts c ON c.owner_id = j.owner_id AND c.chat_id = j.chat_id
                    WHERE j.owner_id = ?
                    ORDER BY j.id DESC
                    """,
                    (owner_id,),
                ).fetchall()
            return [(_row_journal(r), str(r["c_label"] or r["chat_id"]).strip()) for r in rows]

    def get_dashboard(self, owner_id: int, tz_offset_hours: int = 8) -> DashboardStats:
        today_utc = period_start_utc("today", tz_offset_hours)
        with self._lock:
            s = self._conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN balance_tenths > 0 THEN 1 ELSE 0 END) AS pos_c,
                    COALESCE(SUM(CASE WHEN balance_tenths > 0 THEN balance_tenths ELSE 0 END), 0) AS pos_s,
                    SUM(CASE WHEN balance_tenths < 0 THEN 1 ELSE 0 END) AS debt_c,
                    COALESCE(SUM(CASE WHEN balance_tenths < 0 THEN balance_tenths ELSE 0 END), 0) AS debt_s,
                    SUM(CASE WHEN balance_tenths = 0 THEN 1 ELSE 0 END) AS zero_c
                FROM contacts
                WHERE owner_id = ? AND has_ledger = 1
                """,
                (owner_id,),
            ).fetchone()
            t = self._conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN delta_tenths > 0 AND kind IN ('in','adjust') THEN delta_tenths ELSE 0 END), 0) AS tin,
                    COALESCE(SUM(CASE WHEN delta_tenths < 0 AND kind IN ('out','adjust') THEN -delta_tenths ELSE 0 END), 0) AS tout,
                    COUNT(*) AS cnt
                FROM journal
                WHERE owner_id = ? AND created_at >= ? AND voided_by_id IS NULL
                """,
                (owner_id, today_utc),
            ).fetchone()
            pend = int(self._conn.execute("SELECT COUNT(*) FROM receipts WHERE owner_id = ? AND status = 'pending'", (owner_id,)).fetchone()[0])
            acc = self._conn.execute("SELECT auto_pin FROM accounts WHERE owner_id = ?", (owner_id,)).fetchone()
            return DashboardStats(
                total_contacts=int(s["total"] or 0),
                positive_count=int(s["pos_c"] or 0),
                positive_sum=int(s["pos_s"] or 0),
                debt_count=int(s["debt_c"] or 0),
                debt_sum=int(s["debt_s"] or 0),
                zero_count=int(s["zero_c"] or 0),
                today_in=int(t["tin"] or 0),
                today_out=int(t["tout"] or 0),
                today_tx_count=int(t["cnt"] or 0),
                pending_receipts=pend,
                auto_pin=bool(acc["auto_pin"]) if acc else True,
            )

    def get_financial_report(self, owner_id: int, period: Literal["today", "week", "month", "all"], tz_offset_hours: int = 8) -> FinancialReport:
        since = period_start_utc(period, tz_offset_hours)
        with self._lock:
            if since:
                row = self._conn.execute(
                    """
                    SELECT
                        COALESCE(SUM(CASE WHEN delta_tenths > 0 AND kind IN ('in','adjust') THEN delta_tenths ELSE 0 END), 0) AS tin,
                        COALESCE(SUM(CASE WHEN delta_tenths < 0 AND kind IN ('out','adjust') THEN -delta_tenths ELSE 0 END), 0) AS tout,
                        COUNT(*) AS cnt,
                        COUNT(DISTINCT chat_id) AS active_c
                    FROM journal
                    WHERE owner_id = ? AND created_at >= ? AND voided_by_id IS NULL
                    """,
                    (owner_id, since),
                ).fetchone()
            else:
                row = self._conn.execute(
                    """
                    SELECT
                        COALESCE(SUM(CASE WHEN delta_tenths > 0 AND kind IN ('in','adjust') THEN delta_tenths ELSE 0 END), 0) AS tin,
                        COALESCE(SUM(CASE WHEN delta_tenths < 0 AND kind IN ('out','adjust') THEN -delta_tenths ELSE 0 END), 0) AS tout,
                        COUNT(*) AS cnt,
                        COUNT(DISTINCT chat_id) AS active_c
                    FROM journal
                    WHERE owner_id = ? AND voided_by_id IS NULL
                    """,
                    (owner_id,),
                ).fetchone()
            top_pos = [
                _row_contact(r)
                for r in self._conn.execute(
                    "SELECT * FROM contacts WHERE owner_id = ? AND has_ledger = 1 AND balance_tenths > 0 ORDER BY balance_tenths DESC LIMIT 5",
                    (owner_id,),
                ).fetchall()
            ]
            top_debt = [
                _row_contact(r)
                for r in self._conn.execute(
                    "SELECT * FROM contacts WHERE owner_id = ? AND has_ledger = 1 AND balance_tenths < 0 ORDER BY balance_tenths ASC LIMIT 5",
                    (owner_id,),
                ).fetchall()
            ]
            tin = int(row["tin"] or 0)
            tout = int(row["tout"] or 0)
            return FinancialReport(
                period=period,
                total_in=tin,
                total_out=tout,
                net_flow=tin - tout,
                tx_count=int(row["cnt"] or 0),
                active_contacts=int(row["active_c"] or 0),
                top_positive=top_pos,
                top_debt=top_debt,
            )

    def list_contacts(
        self,
        owner_id: int,
        mode: str,
        limit: int = 10,
        offset: int = 0,
        tz_offset_hours: int = 8,
    ) -> tuple[list[Contact], int]:
        where = "owner_id = ? AND has_ledger = 1"
        params: list[object] = [owner_id]
        order = "updated_at DESC, chat_id ASC"

        if mode == "positive":
            where += " AND balance_tenths > 0"
            order = "balance_tenths DESC, updated_at DESC"
        elif mode == "debt":
            where += " AND balance_tenths < 0"
            order = "balance_tenths ASC, updated_at DESC"
        elif mode == "today":
            today_utc = period_start_utc("today", tz_offset_hours)
            where += " AND updated_at >= ?"
            params.append(today_utc)
        elif mode == "archived":
            where += " AND archived = 1"
        elif mode.startswith("tag:"):
            tag = mode[4:]
            where += " AND (',' || tags || ',') LIKE ?"
            params.append(f"%,{tag},%")

        with self._lock:
            total = int(self._conn.execute(f"SELECT COUNT(*) FROM contacts WHERE {where}", params).fetchone()[0])
            rows = self._conn.execute(
                f"SELECT * FROM contacts WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            return ([_row_contact(r) for r in rows], total)

    def list_all_contacts_for_export(self, owner_id: int) -> list[Contact]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM contacts WHERE owner_id = ? AND has_ledger = 1 ORDER BY balance_tenths DESC, updated_at DESC",
                (owner_id,),
            ).fetchall()
            return [_row_contact(r) for r in rows]

    def search_contacts(self, owner_id: int, query: str, limit: int = 10, offset: int = 0) -> tuple[list[Contact], int]:
        q = query.strip()
        if not q or q == "@" or len(q) > 64:
            return ([], 0)
        like = f"%{q.lstrip('@').lower()}%"
        where = """
            owner_id = ? AND has_ledger = 1 AND (
                CAST(chat_id AS TEXT) LIKE ?
                OR LOWER(COALESCE(alias, '')) LIKE ?
                OR LOWER(COALESCE(tags, '')) LIKE ?
                OR LOWER(COALESCE(tg_username, '')) LIKE ?
                OR LOWER(COALESCE(tg_first_name, '')) LIKE ?
                OR LOWER(COALESCE(tg_last_name, '')) LIKE ?
                OR LOWER(TRIM(COALESCE(tg_first_name, '') || ' ' || COALESCE(tg_last_name, ''))) LIKE ?
            )
        """
        params = (owner_id, like, like, like, like, like, like, like)
        with self._lock:
            total = int(self._conn.execute(f"SELECT COUNT(*) FROM contacts WHERE {where}", params).fetchone()[0])
            rows = self._conn.execute(
                f"SELECT * FROM contacts WHERE {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            return ([_row_contact(r) for r in rows], total)

    def list_owner_tags(self, owner_id: int) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT tags FROM contacts WHERE owner_id = ? AND has_ledger = 1 AND tags != ''",
                (owner_id,),
            ).fetchall()
        tags: set[str] = set()
        for r in rows:
            tags.update(_parse_tags(r["tags"]))
        return sorted(tags)

    # ------------------------------------------------------------------
    # Receipts Outbox
    # ------------------------------------------------------------------

    def get_due_receipts(self, limit: int = 20) -> list[ReceiptItem]:
        now = utc_now_iso()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM receipts WHERE status = 'pending' AND next_try_at <= ? ORDER BY next_try_at ASC, id ASC LIMIT ?",
                (now, limit),
            ).fetchall()
            return [_row_receipt(r) for r in rows]

    def has_due_receipts(self) -> bool:
        now = utc_now_iso()
        with self._lock:
            return (
                self._conn.execute("SELECT 1 FROM receipts WHERE status = 'pending' AND next_try_at <= ? LIMIT 1", (now,)).fetchone()
                is not None
            )

    def list_pending_receipts(self, owner_id: int, limit: int = 10, offset: int = 0) -> tuple[list[ReceiptItem], int]:
        with self._lock:
            total = int(self._conn.execute("SELECT COUNT(*) FROM receipts WHERE owner_id = ? AND status = 'pending'", (owner_id,)).fetchone()[0])
            rows = self._conn.execute(
                "SELECT * FROM receipts WHERE owner_id = ? AND status = 'pending' ORDER BY id ASC LIMIT ? OFFSET ?",
                (owner_id, limit, offset),
            ).fetchall()
            return ([_row_receipt(r) for r in rows], total)

    def mark_receipt_sent(self, receipt_id: int) -> None:
        now = utc_now_iso()
        with self._tx() as conn:
            conn.execute("UPDATE receipts SET status = 'sent', sent_at = ?, last_error = NULL WHERE id = ?", (now, receipt_id))

    def mark_receipt_retry(self, receipt_id: int, delay_seconds: float, error_text: str) -> None:
        nxt = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        with self._tx() as conn:
            conn.execute(
                "UPDATE receipts SET attempts = attempts + 1, next_try_at = ?, last_error = ? WHERE id = ?",
                (nxt, error_text[:300], receipt_id),
            )

    def switch_receipt_link(self, receipt_id: int, link_id: str) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE receipts SET link_id = ? WHERE id = ? AND status = 'pending'", (link_id, receipt_id))

    def retry_pending_receipts(self, owner_id: int) -> int:
        now = utc_now_iso()
        with self._tx() as conn:
            cur = conn.execute("UPDATE receipts SET next_try_at = ? WHERE owner_id = ? AND status = 'pending'", (now, owner_id))
            return cur.rowcount

    # ------------------------------------------------------------------
    # Announcements (Targeted Broadcasts)
    # ------------------------------------------------------------------

    def select_announcement_targets(self, owner_id: int, target_key: str) -> list[Contact]:
        if target_key == "all":
            where, params = "owner_id = ? AND has_ledger = 1", [owner_id]
        elif target_key == "positive":
            where, params = "owner_id = ? AND has_ledger = 1 AND balance_tenths > 0", [owner_id]
        elif target_key == "non_negative":
            where, params = "owner_id = ? AND has_ledger = 1 AND balance_tenths >= 0", [owner_id]
        elif target_key == "debt":
            where, params = "owner_id = ? AND has_ledger = 1 AND balance_tenths < 0", [owner_id]
        elif target_key.startswith("tag:"):
            tag = target_key[4:]
            where, params = "owner_id = ? AND has_ledger = 1 AND (',' || tags || ',') LIKE ?", [owner_id, f"%,{tag},%"]
        else:
            return []
        with self._lock:
            rows = self._conn.execute(f"SELECT * FROM contacts WHERE {where} ORDER BY updated_at DESC LIMIT 1001", params).fetchall()
            return [_row_contact(r) for r in rows]

    def create_announcement(self, owner_id: int, target_desc: str, body_text: str, chat_ids: list[int]) -> int:
        now = utc_now_iso()
        with self._tx() as conn:
            if conn.execute("SELECT 1 FROM announcements WHERE owner_id = ? AND status IN ('running','paused')", (owner_id,)).fetchone():
                raise RuntimeError("Active announcement already exists")
            cur = conn.execute(
                "INSERT INTO announcements(owner_id, target_desc, body_text, status, created_at, updated_at) VALUES (?, ?, ?, 'running', ?, ?)",
                (owner_id, target_desc, body_text, now, now),
            )
            ann_id = int(cur.lastrowid)
            conn.executemany(
                "INSERT INTO announcement_deliveries(ann_id, chat_id, status, attempts, next_try_at, updated_at) VALUES (?, ?, 'pending', 0, ?, ?)",
                [(ann_id, cid, now, now) for cid in chat_ids],
            )
            return ann_id

    def get_latest_announcement(self, owner_id: int) -> AnnouncementJob | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM announcements WHERE owner_id = ? ORDER BY id DESC LIMIT 1", (owner_id,)).fetchone()
            if not r:
                return None
            return AnnouncementJob(
                id=int(r["id"]),
                owner_id=int(r["owner_id"]),
                target_desc=str(r["target_desc"]),
                body_text=str(r["body_text"]),
                status=str(r["status"]),
                created_at=str(r["created_at"]),
                updated_at=str(r["updated_at"]),
            )

    def recover_announcements_on_startup(self) -> None:
        now = utc_now_iso()
        with self._tx() as conn:
            conn.execute(
                "UPDATE announcement_deliveries SET status = 'uncertain', error_text = 'Restarted while sending', updated_at = ? WHERE status = 'sending'",
                (now,),
            )
            conn.execute("UPDATE announcements SET status = 'paused', updated_at = ? WHERE status = 'running'", (now,))

    def set_announcement_status(self, ann_id: int, owner_id: int, status: Literal["running", "paused", "cancelled"]) -> bool:
        now = utc_now_iso()
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE announcements SET status = ?, updated_at = ? WHERE id = ? AND owner_id = ? AND status IN ('running', 'paused')",
                (status, now, ann_id, owner_id),
            )
            if cur.rowcount == 0:
                return False
            if status == "cancelled":
                conn.execute(
                    "UPDATE announcement_deliveries SET status = 'cancelled', updated_at = ? WHERE ann_id = ? AND status = 'pending'",
                    (now, ann_id),
                )
            return True

    def claim_next_delivery(self) -> tuple[AnnouncementJob, AnnouncementDelivery] | None:
        now = utc_now_iso()
        with self._tx() as conn:
            r = conn.execute(
                """
                SELECT d.*, a.owner_id, a.target_desc, a.body_text, a.status AS a_status,
                       a.created_at AS a_created, a.updated_at AS a_updated
                FROM announcement_deliveries d
                JOIN announcements a ON a.id = d.ann_id
                WHERE a.status = 'running' AND d.status = 'pending' AND d.next_try_at <= ?
                ORDER BY d.next_try_at ASC, d.id ASC LIMIT 1
                """,
                (now,),
            ).fetchone()
            if not r:
                conn.execute(
                    """
                    UPDATE announcements SET status = 'done', updated_at = ?
                    WHERE status = 'running' AND NOT EXISTS (
                        SELECT 1 FROM announcement_deliveries d WHERE d.ann_id = announcements.id AND d.status IN ('pending','sending')
                    )
                    """,
                    (now,),
                )
                return None
            conn.execute("UPDATE announcement_deliveries SET status = 'sending', attempts = attempts + 1, updated_at = ? WHERE id = ?", (now, r["id"]))
            job = AnnouncementJob(
                id=int(r["ann_id"]),
                owner_id=int(r["owner_id"]),
                target_desc=str(r["target_desc"]),
                body_text=str(r["body_text"]),
                status=str(r["a_status"]),
                created_at=str(r["a_created"]),
                updated_at=str(r["a_updated"]),
            )
            dlv = AnnouncementDelivery(
                id=int(r["id"]),
                ann_id=int(r["ann_id"]),
                chat_id=int(r["chat_id"]),
                status="sending",
                attempts=int(r["attempts"]) + 1,
                next_try_at=str(r["next_try_at"]),
                tg_sent_msg_id=r["tg_sent_msg_id"],
                error_text=r["error_text"],
                updated_at=now,
            )
            return (job, dlv)

    def finish_delivery(
        self,
        delivery_id: int,
        status: Literal["sent", "failed", "uncertain", "pending"],
        *,
        tg_msg_id: int | None = None,
        error_text: str | None = None,
        delay_seconds: float | None = None,
    ) -> None:
        now = utc_now_iso()
        nxt = now
        if delay_seconds is not None:
            nxt = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE announcement_deliveries
                SET status = ?, tg_sent_msg_id = COALESCE(?, tg_sent_msg_id),
                    error_text = ?, next_try_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, tg_msg_id, error_text[:300] if error_text else None, nxt, now, delivery_id),
            )

    def get_announcement_counts(self, ann_id: int) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM announcement_deliveries WHERE ann_id = ? GROUP BY status",
                (ann_id,),
            ).fetchall()
        out = {"pending": 0, "sending": 0, "sent": 0, "failed": 0, "uncertain": 0, "cancelled": 0}
        for r in rows:
            out[str(r["status"])] = int(r["cnt"])
        return out
