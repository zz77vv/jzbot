"""Lossless read-only migrator: jzbot (V2 ledger.db OR V4 jzbot.db) -> LedgerFlow ledgerflow.db.

Usage:
    python tools/migrate.py /path/to/source.db /path/to/ledgerflow.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ledgerflow.database import open_database


@dataclass(frozen=True)
class MigrateSummary:
    source_format: str
    accounts: int
    links: int
    contacts: int
    ledgers: int
    journal_entries: int
    handled_msgs: int
    receipts: int
    net_balance_tenths: int


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def migrate_to_ledgerflow(src_path: Path, dst_path: Path) -> MigrateSummary:
    src_path = src_path.expanduser().resolve()
    dst_path = dst_path.expanduser().resolve()
    if not src_path.is_file():
        raise FileNotFoundError(f"Source DB not found: {src_path}")
    if dst_path.exists():
        raise FileExistsError(f"Target DB already exists: {dst_path}")

    src = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    dst = open_database(dst_path)

    try:
        if _has_table(src, "owners") and _has_table(src, "transactions") and _has_table(src, "customers"):
            summary = _migrate_from_v4(src, dst)
        elif _has_table(src, "business_accounts") and _has_table(src, "wallets") and _has_table(src, "ledger"):
            summary = _migrate_from_v2(src, dst)
        else:
            raise RuntimeError("Unrecognized source database schema")

        qc = dst.execute("PRAGMA quick_check").fetchone()[0]
        fk = dst.execute("PRAGMA foreign_key_check").fetchall()
        if qc != "ok" or fk:
            raise RuntimeError(f"Target integrity failed: quick_check={qc}, fk_errors={len(fk)}")
        return summary
    except Exception:
        if dst.in_transaction:
            dst.execute("ROLLBACK")
        dst.close()
        dst_path.unlink(missing_ok=True)
        raise
    finally:
        src.close()
        try:
            dst.close()
        except Exception:
            pass


def _migrate_from_v4(src: sqlite3.Connection, dst: sqlite3.Connection) -> MigrateSummary:
    dst.execute("BEGIN IMMEDIATE")

    owners = src.execute("SELECT owner_id, welcome_sent, created_at, updated_at FROM owners").fetchall()
    dst.executemany(
        "INSERT INTO accounts(owner_id, welcomed, auto_pin, created_at, updated_at) VALUES (?, ?, 1, ?, ?)",
        [(int(r["owner_id"]), int(r["welcome_sent"]), str(r["created_at"]), str(r["updated_at"])) for r in owners],
    )

    links = src.execute("SELECT connection_id, owner_id, is_enabled, updated_at FROM connections").fetchall()
    dst.executemany(
        "INSERT INTO bus_links(link_id, owner_id, enabled, updated_at) VALUES (?, ?, ?, ?)",
        [(str(r["connection_id"]), int(r["owner_id"]), int(r["is_enabled"]), str(r["updated_at"])) for r in links],
    )

    customers = src.execute("SELECT * FROM customers").fetchall()
    expected_balances: dict[tuple[int, int], int] = {}
    for r in customers:
        oid, cid = int(r["owner_id"]), int(r["chat_id"])
        has_w = int(r["has_wallet"])
        bal = int(r["balance_tenths"])
        if has_w:
            expected_balances[(oid, cid)] = bal
        dst.execute(
            """
            INSERT INTO contacts(
                owner_id, chat_id, alias, tags, tg_username, tg_first_name, tg_last_name,
                has_ledger, balance_tenths, total_in_tenths, total_out_tenths, tx_count,
                archived, created_at, updated_at
            )
            VALUES (?, ?, NULL, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                oid,
                cid,
                r["username"],
                r["first_name"],
                r["last_name"],
                has_w,
                bal,
                int(r["deposit_total_tenths"]),
                int(r["withdraw_total_tenths"]),
                int(r["entry_count"]),
                int(r["is_disabled"]),
                str(r["created_at"]),
                str(r["updated_at"]),
            ),
        )

    txs = src.execute("SELECT * FROM transactions ORDER BY id ASC").fetchall()
    for r in txs:
        old_kind = str(r["kind"])
        amt = int(r["amount_tenths"])
        before = int(r["balance_before"])
        after = int(r["balance_after"])
        delta = after - before
        if old_kind == "deposit":
            kind, note = "in", ""
        elif old_kind == "withdraw":
            kind, note = "out", ""
        elif old_kind == "clear":
            kind, note = "settle", "清账"
        else:
            kind, note = "settle", "后台归零"
        dst.execute(
            """
            INSERT INTO journal(
                owner_id, chat_id, kind, delta_tenths, amount_tenths,
                balance_before, balance_after, note, tg_msg_id, actor_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(r["owner_id"]),
                int(r["chat_id"]),
                kind,
                delta,
                amt,
                before,
                after,
                note,
                int(r["source_message_id"]) if r["source_message_id"] is not None else None,
                int(r["operator_id"]),
                str(r["created_at"]),
            ),
        )

    pms = src.execute("SELECT * FROM processed_messages").fetchall()
    dst.executemany(
        "INSERT INTO handled_msgs(owner_id, chat_id, tg_msg_id, intent, handled_at) VALUES (?, ?, ?, ?, ?)",
        [(int(r["owner_id"]), int(r["chat_id"]), int(r["source_message_id"]), str(r["command_type"]), str(r["processed_at"])) for r in pms],
    )

    rcpts = src.execute("SELECT * FROM receipt_outbox ORDER BY id ASC").fetchall()
    dst.executemany(
        """
        INSERT OR IGNORE INTO receipts(
            owner_id, chat_id, tg_msg_id, link_id, html_text,
            reply_msg_id, should_pin, status, attempts, next_try_at,
            created_at, sent_at, last_error
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                int(r["owner_id"]),
                int(r["chat_id"]),
                int(r["source_message_id"]),
                str(r["connection_id"]),
                str(r["receipt_text"]),
                int(r["reply_to_message_id"]) if r["reply_to_message_id"] is not None else None,
                int(r["should_pin"]),
                "sent" if str(r["status"]) == "delivered" else "pending",
                int(r["attempt_count"]),
                str(r["next_attempt_at"]),
                str(r["created_at"]),
                r["delivered_at"],
                r["last_error"],
            )
            for r in rcpts
        ],
    )

    dst.execute("COMMIT")

    # Strict reconciliation
    actual_balances = {
        (int(r["owner_id"]), int(r["chat_id"])): int(r["balance_tenths"])
        for r in dst.execute("SELECT owner_id, chat_id, balance_tenths FROM contacts WHERE has_ledger = 1").fetchall()
    }
    if actual_balances != expected_balances:
        raise RuntimeError("Reconciliation mismatch on contact balances!")
    j_cnt = int(dst.execute("SELECT COUNT(*) FROM journal").fetchone()[0])
    if j_cnt != len(txs):
        raise RuntimeError(f"Journal count mismatch: {j_cnt} != {len(txs)}")

    return MigrateSummary(
        source_format="v4",
        accounts=len(owners),
        links=len(links),
        contacts=len(customers),
        ledgers=len(actual_balances),
        journal_entries=j_cnt,
        handled_msgs=len(pms),
        receipts=int(dst.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]),
        net_balance_tenths=sum(actual_balances.values()),
    )


def _migrate_from_v2(src: sqlite3.Connection, dst: sqlite3.Connection) -> MigrateSummary:
    dst.execute("BEGIN IMMEDIATE")

    owners = src.execute("SELECT owner_user_id, welcome_sent, first_seen_at, last_seen_at FROM business_accounts").fetchall()
    dst.executemany(
        "INSERT INTO accounts(owner_id, welcomed, auto_pin, created_at, updated_at) VALUES (?, ?, 1, ?, ?)",
        [(int(r["owner_user_id"]), int(r["welcome_sent"]), str(r["first_seen_at"]), str(r["last_seen_at"])) for r in owners],
    )

    links = src.execute("SELECT connection_id, owner_user_id, is_enabled, updated_at FROM business_connections").fetchall()
    dst.executemany(
        "INSERT INTO bus_links(link_id, owner_id, enabled, updated_at) VALUES (?, ?, ?, ?)",
        [(str(r["connection_id"]), int(r["owner_user_id"]), int(r["is_enabled"]), str(r["updated_at"])) for r in links],
    )

    profiles = {(int(r["owner_user_id"]), int(r["customer_chat_id"])): r for r in src.execute("SELECT * FROM customer_profiles").fetchall()} if _has_table(src, "customer_profiles") else {}
    wallets = {(int(r["owner_user_id"]), int(r["customer_chat_id"])): r for r in src.execute("SELECT * FROM wallets").fetchall()}
    stats = {(int(r["owner_user_id"]), int(r["customer_chat_id"])): r for r in src.execute("SELECT * FROM wallet_stats").fetchall()} if _has_table(src, "wallet_stats") else {}
    mgmt = {(int(r["owner_user_id"]), int(r["customer_chat_id"])): int(r["is_disabled"]) for r in src.execute("SELECT owner_user_id, customer_chat_id, is_disabled FROM customer_management").fetchall()} if _has_table(src, "customer_management") else {}

    all_keys = sorted(set(profiles.keys()) | set(wallets.keys()))
    expected_balances = {k: int(v["balance_tenths"]) for k, v in wallets.items()}
    for oid, cid in all_keys:
        p, w, st = profiles.get((oid, cid)), wallets.get((oid, cid)), stats.get((oid, cid))
        created = str(w["created_at"]) if w else str(p["updated_at"])
        updated = str(w["updated_at"]) if w else str(p["updated_at"])
        dst.execute(
            """
            INSERT INTO contacts(
                owner_id, chat_id, alias, tags, tg_username, tg_first_name, tg_last_name,
                has_ledger, balance_tenths, total_in_tenths, total_out_tenths, tx_count,
                archived, created_at, updated_at
            )
            VALUES (?, ?, NULL, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                oid,
                cid,
                p["username"] if p else None,
                p["first_name"] if p else None,
                p["last_name"] if p else None,
                1 if w else 0,
                int(w["balance_tenths"]) if w else 0,
                int(st["deposit_total_tenths"]) if st else 0,
                int(st["withdraw_total_tenths"]) if st else 0,
                int(st["entry_count"]) if st else 0,
                mgmt.get((oid, cid), 0),
                created,
                updated,
            ),
        )

    items: list[tuple[int, int, int, tuple]] = []
    for r in src.execute("SELECT * FROM ledger ORDER BY id ASC").fetchall():
        et = str(r["entry_type"])
        before, after = int(r["balance_before"]), int(r["balance_after"])
        kind = "in" if et == "deposit" else ("out" if et == "withdraw" else "settle")
        note = "清账" if et == "clear" else ""
        items.append((int(r["id"]), 0, int(r["id"]), (int(r["owner_user_id"]), int(r["customer_chat_id"]), kind, after - before, int(r["amount_tenths"]), before, after, note, int(r["source_message_id"]), int(r["owner_user_id"]), str(r["created_at_utc"]))))

    if _has_table(src, "admin_operations"):
        for r in src.execute("SELECT * FROM admin_operations WHERE result = 'applied' ORDER BY ledger_anchor_id ASC, id ASC").fetchall():
            before, after = int(r["balance_before"] or 0), int(r["balance_after"] or 0)
            items.append((int(r["ledger_anchor_id"] or 0), 1, int(r["id"]), (int(r["owner_user_id"]), int(r["customer_chat_id"]), "settle", after - before, abs(before), before, after, "后台归零", None, int(r["administrator_id"]), str(r["completed_at_utc"] or r["created_at_utc"]))))

    items.sort(key=lambda x: (x[0], x[1], x[2]))
    dst.executemany(
        """
        INSERT INTO journal(
            owner_id, chat_id, kind, delta_tenths, amount_tenths,
            balance_before, balance_after, note, tg_msg_id, actor_id, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [x[3] for x in items],
    )
    dst.execute(
        "UPDATE contacts SET tx_count = (SELECT COUNT(*) FROM journal j WHERE j.owner_id = contacts.owner_id AND j.chat_id = contacts.chat_id) WHERE has_ledger = 1"
    )

    pms = src.execute("SELECT owner_user_id, customer_chat_id, source_message_id, command_type, processed_at_utc FROM processed_messages").fetchall()
    dst.executemany(
        "INSERT INTO handled_msgs(owner_id, chat_id, tg_msg_id, intent, handled_at) VALUES (?, ?, ?, ?, ?)",
        [(int(r["owner_user_id"]), int(r["customer_chat_id"]), int(r["source_message_id"]), str(r["command_type"]), str(r["processed_at_utc"])) for r in pms],
    )

    rcpts = src.execute("SELECT * FROM receipt_outbox ORDER BY id ASC").fetchall()
    dst.executemany(
        """
        INSERT OR IGNORE INTO receipts(
            owner_id, chat_id, tg_msg_id, link_id, html_text,
            reply_msg_id, should_pin, status, attempts, next_try_at,
            created_at, sent_at, last_error
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                int(r["owner_user_id"]),
                int(r["customer_chat_id"]),
                int(r["source_message_id"]),
                str(r["business_connection_id"]),
                str(r["receipt_text"]),
                int(r["reply_to_message_id"]) if r["reply_to_message_id"] is not None else None,
                int(r["should_pin"]),
                "sent" if str(r["status"]) == "delivered" else "pending",
                int(r["attempt_count"]),
                str(r["next_attempt_at_utc"]),
                str(r["created_at_utc"]),
                r["delivered_at_utc"],
                r["last_error"],
            )
            for r in rcpts
        ],
    )

    dst.execute("COMMIT")

    actual_balances = {
        (int(r["owner_id"]), int(r["chat_id"])): int(r["balance_tenths"])
        for r in dst.execute("SELECT owner_id, chat_id, balance_tenths FROM contacts WHERE has_ledger = 1").fetchall()
    }
    if actual_balances != expected_balances:
        raise RuntimeError("Reconciliation mismatch on V2 contact balances!")

    return MigrateSummary(
        source_format="v2",
        accounts=len(owners),
        links=len(links),
        contacts=len(all_keys),
        ledgers=len(actual_balances),
        journal_entries=len(items),
        handled_msgs=len(pms),
        receipts=int(dst.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]),
        net_balance_tenths=sum(actual_balances.values()),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate legacy jzbot DB into LedgerFlow DB")
    parser.add_argument("source_db", type=Path)
    parser.add_argument("target_db", type=Path)
    args = parser.parse_args()
    s = migrate_to_ledgerflow(args.source_db, args.target_db)
    print(f"Migration ({s.source_format} -> ledgerflow) PASSED:")
    print(f"  Accounts:        {s.accounts}")
    print(f"  Business links:  {s.links}")
    print(f"  Contacts:        {s.contacts} (ledgers: {s.ledgers})")
    print(f"  Journal entries: {s.journal_entries}")
    print(f"  Handled msgs:    {s.handled_msgs}")
    print(f"  Receipts:        {s.receipts}")
    print(f"  Net tenths:      {s.net_balance_tenths}")


if __name__ == "__main__":
    main()
