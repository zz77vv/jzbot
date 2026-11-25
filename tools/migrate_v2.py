"""Verified read-only migration tool: legacy V2 ledger.db -> redesigned JZBot SQLite DB.

Usage:
    python tools/migrate_v2.py /path/to/old_ledger.db /path/to/new_jzbot.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

# Allow running directly as `python tools/migrate_v2.py`
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from jzbot.schema import connect_db


@dataclass(frozen=True)
class MigrationReport:
    owners: int
    connections: int
    customers_total: int
    wallets: int
    transactions: int
    processed_messages: int
    receipts: int
    total_balance_tenths: int


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def migrate_v2_to_new(src_path: Path, dst_path: Path) -> MigrationReport:
    src_path = src_path.expanduser().resolve()
    dst_path = dst_path.expanduser().resolve()

    if not src_path.is_file():
        raise FileNotFoundError(f"Source V2 database not found: {src_path}")
    if dst_path.exists():
        raise FileExistsError(f"Target database already exists (refusing to overwrite): {dst_path}")

    src = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    dst = connect_db(dst_path)

    try:
        # Verify expected V2 core tables exist
        for required in ("business_accounts", "business_connections", "wallets", "ledger", "processed_messages", "receipt_outbox"):
            if not _table_exists(src, required):
                raise RuntimeError(f"Source DB missing required V2 table: {required}")

        has_profiles = _table_exists(src, "customer_profiles")
        has_stats = _table_exists(src, "wallet_stats")
        has_mgmt = _table_exists(src, "customer_management")
        has_admin_ops = _table_exists(src, "admin_operations")

        dst.execute("BEGIN IMMEDIATE")

        # 1. Owners
        owners_rows = src.execute(
            "SELECT owner_user_id, welcome_sent, first_seen_at, last_seen_at FROM business_accounts"
        ).fetchall()
        dst.executemany(
            "INSERT INTO owners(owner_id, welcome_sent, created_at, updated_at) VALUES (?, ?, ?, ?)",
            [(int(r["owner_user_id"]), int(r["welcome_sent"]), str(r["first_seen_at"]), str(r["last_seen_at"])) for r in owners_rows],
        )

        # 2. Connections
        conn_rows = src.execute(
            "SELECT connection_id, owner_user_id, is_enabled, updated_at FROM business_connections"
        ).fetchall()
        dst.executemany(
            "INSERT INTO connections(connection_id, owner_id, is_enabled, updated_at) VALUES (?, ?, ?, ?)",
            [(str(r["connection_id"]), int(r["owner_user_id"]), int(r["is_enabled"]), str(r["updated_at"])) for r in conn_rows],
        )

        # 3. Unified customers (merge profiles + wallets + wallet_stats + customer_management)
        profiles: dict[tuple[int, int], sqlite3.Row] = {}
        if has_profiles:
            for r in src.execute("SELECT * FROM customer_profiles").fetchall():
                profiles[(int(r["owner_user_id"]), int(r["customer_chat_id"]))] = r

        wallets: dict[tuple[int, int], sqlite3.Row] = {}
        for r in src.execute("SELECT * FROM wallets").fetchall():
            wallets[(int(r["owner_user_id"]), int(r["customer_chat_id"]))] = r

        stats: dict[tuple[int, int], sqlite3.Row] = {}
        if has_stats:
            for r in src.execute("SELECT * FROM wallet_stats").fetchall():
                stats[(int(r["owner_user_id"]), int(r["customer_chat_id"]))] = r

        disabled_map: dict[tuple[int, int], int] = {}
        if has_mgmt:
            for r in src.execute("SELECT owner_user_id, customer_chat_id, is_disabled FROM customer_management").fetchall():
                disabled_map[(int(r["owner_user_id"]), int(r["customer_chat_id"]))] = int(r["is_disabled"])

        all_customer_keys = set(profiles.keys()) | set(wallets.keys())
        # Also include any (owner, chat) appearing in ledger or applied admin_operations so FK constraints never fail
        for r in src.execute("SELECT DISTINCT owner_user_id, customer_chat_id FROM ledger").fetchall():
            all_customer_keys.add((int(r[0]), int(r[1])))
        if has_admin_ops:
            for r in src.execute(
                "SELECT DISTINCT owner_user_id, customer_chat_id FROM admin_operations WHERE result = 'applied'"
            ).fetchall():
                all_customer_keys.add((int(r[0]), int(r[1])))

        for owner_id, chat_id in sorted(all_customer_keys):
            p = profiles.get((owner_id, chat_id))
            w = wallets.get((owner_id, chat_id))
            st = stats.get((owner_id, chat_id))
            is_dis = disabled_map.get((owner_id, chat_id), 0)

            created_at = str(w["created_at"]) if w else (str(p["updated_at"]) if p else "1970-01-01T00:00:00Z")
            updated_at = str(w["updated_at"]) if w else (str(p["updated_at"]) if p else "1970-01-01T00:00:00Z")
            dst.execute(
                """
                INSERT INTO customers(
                    owner_id, chat_id, username, first_name, last_name,
                    has_wallet, balance_tenths, deposit_total_tenths,
                    withdraw_total_tenths, entry_count, is_disabled,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner_id,
                    chat_id,
                    p["username"] if p else None,
                    p["first_name"] if p else None,
                    p["last_name"] if p else None,
                    1 if w else 0,
                    int(w["balance_tenths"]) if w else 0,
                    int(st["deposit_total_tenths"]) if st else 0,
                    int(st["withdraw_total_tenths"]) if st else 0,
                    int(st["entry_count"]) if st else 0,
                    is_dis,
                    created_at,
                    updated_at,
                ),
            )

        # 4. Unified transactions: merge `ledger` and applied `admin_operations` in chronological order
        tx_items: list[tuple[int, int, int, tuple]] = []
        ledger_rows = src.execute("SELECT * FROM ledger ORDER BY id ASC").fetchall()
        for r in ledger_rows:
            # Sort key: (anchor_ledger_id, is_admin_op=0, sub_id)
            tx_items.append(
                (
                    int(r["id"]),
                    0,
                    int(r["id"]),
                    (
                        int(r["owner_user_id"]),
                        int(r["customer_chat_id"]),
                        str(r["entry_type"]),
                        int(r["amount_tenths"]),
                        int(r["balance_before"]),
                        int(r["balance_after"]),
                        int(r["source_message_id"]),
                        int(r["owner_user_id"]),
                        str(r["created_at_utc"]),
                    ),
                )
            )

        admin_clear_count = 0
        if has_admin_ops:
            admin_rows = src.execute(
                "SELECT * FROM admin_operations WHERE result = 'applied' ORDER BY ledger_anchor_id ASC, id ASC"
            ).fetchall()
            for r in admin_rows:
                admin_clear_count += 1
                anchor = int(r["ledger_anchor_id"] or 0)
                before = int(r["balance_before"] or 0)
                after = int(r["balance_after"] or 0)
                ts = str(r["completed_at_utc"] or r["created_at_utc"])
                tx_items.append(
                    (
                        anchor,
                        1,
                        int(r["id"]),
                        (
                            int(r["owner_user_id"]),
                            int(r["customer_chat_id"]),
                            "admin_clear",
                            abs(before),
                            before,
                            after,
                            None,
                            int(r["administrator_id"]),
                            ts,
                        ),
                    )
                )

        tx_items.sort(key=lambda item: (item[0], item[1], item[2]))
        dst.executemany(
            """
            INSERT INTO transactions(
                owner_id, chat_id, kind, amount_tenths,
                balance_before, balance_after, source_message_id,
                operator_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [item[3] for item in tx_items],
        )

        # Ensure entry_count includes migrated admin_clear rows if wallet_stats lacked them
        if admin_clear_count > 0:
            dst.execute(
                """
                UPDATE customers
                SET entry_count = (
                    SELECT COUNT(*) FROM transactions t
                    WHERE t.owner_id = customers.owner_id AND t.chat_id = customers.chat_id
                )
                WHERE has_wallet = 1
                """
            )

        # 5. Processed messages
        pm_rows = src.execute(
            "SELECT owner_user_id, customer_chat_id, source_message_id, command_type, processed_at_utc FROM processed_messages"
        ).fetchall()
        dst.executemany(
            """
            INSERT INTO processed_messages(owner_id, chat_id, source_message_id, command_type, processed_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    int(r["owner_user_id"]),
                    int(r["customer_chat_id"]),
                    int(r["source_message_id"]),
                    str(r["command_type"]),
                    str(r["processed_at_utc"]),
                )
                for r in pm_rows
            ],
        )

        # 6. Receipt outbox
        outbox_rows = src.execute("SELECT * FROM receipt_outbox ORDER BY id ASC").fetchall()
        dst.executemany(
            """
            INSERT INTO receipt_outbox(
                owner_id, chat_id, source_message_id, connection_id,
                receipt_type, receipt_text, reply_to_message_id, should_pin,
                status, attempt_count, created_at, next_attempt_at,
                delivered_at, last_error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    int(r["owner_user_id"]),
                    int(r["customer_chat_id"]),
                    int(r["source_message_id"]),
                    str(r["business_connection_id"]),
                    str(r["receipt_type"]),
                    str(r["receipt_text"]),
                    int(r["reply_to_message_id"]) if r["reply_to_message_id"] is not None else None,
                    int(r["should_pin"]),
                    str(r["status"]),
                    int(r["attempt_count"]),
                    str(r["created_at_utc"]),
                    str(r["next_attempt_at_utc"]),
                    r["delivered_at_utc"],
                    r["last_error"],
                )
                for r in outbox_rows
            ],
        )

        dst.execute("COMMIT")

        # ------------------------------------------------------------------
        # Strict post-migration reconciliation
        # ------------------------------------------------------------------
        qc = dst.execute("PRAGMA quick_check").fetchone()[0]
        fk = dst.execute("PRAGMA foreign_key_check").fetchall()
        if qc != "ok" or fk:
            raise RuntimeError(f"Target DB integrity failed: quick_check={qc}, fk_errors={len(fk)}")

        dst_wallets = {
            (int(r["owner_id"]), int(r["chat_id"])): int(r["balance_tenths"])
            for r in dst.execute("SELECT owner_id, chat_id, balance_tenths FROM customers WHERE has_wallet = 1").fetchall()
        }
        src_wallets = {k: int(v["balance_tenths"]) for k, v in wallets.items()}
        if dst_wallets != src_wallets:
            raise RuntimeError("Reconciliation failed: migrated wallet balances do not match V2 source")

        dst_tx_count = int(dst.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
        expected_tx_count = len(ledger_rows) + admin_clear_count
        if dst_tx_count != expected_tx_count:
            raise RuntimeError(f"Reconciliation failed: transactions {dst_tx_count} != {expected_tx_count}")

        return MigrationReport(
            owners=len(owners_rows),
            connections=len(conn_rows),
            customers_total=len(all_customer_keys),
            wallets=len(wallets),
            transactions=dst_tx_count,
            processed_messages=len(pm_rows),
            receipts=len(outbox_rows),
            total_balance_tenths=sum(dst_wallets.values()),
        )
    except Exception:
        if dst.in_transaction:
            dst.execute("ROLLBACK")
        dst.close()
        if dst_path.exists():
            dst_path.unlink()
        raise
    finally:
        src.close()
        try:
            dst.close()
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate legacy V2 ledger.db to redesigned JZBot SQLite DB")
    parser.add_argument("source_v2_db", type=Path, help="Path to legacy V2 ledger.db (opened read-only)")
    parser.add_argument("target_new_db", type=Path, help="Path to new jzbot.db (must not already exist)")
    args = parser.parse_args()

    report = migrate_v2_to_new(args.source_v2_db, args.target_new_db)
    print("Migration & reconciliation PASSED:")
    print(f"  Owners:             {report.owners}")
    print(f"  Connections:        {report.connections}")
    print(f"  Customers (total):  {report.customers_total} (wallets: {report.wallets})")
    print(f"  Transactions:       {report.transactions}")
    print(f"  Processed messages: {report.processed_messages}")
    print(f"  Receipts:           {report.receipts}")
    print(f"  Net balance tenths: {report.total_balance_tenths}")


if __name__ == "__main__":
    main()
