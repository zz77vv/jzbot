"""Tests for lossless migration from V4 jzbot.db into LedgerFlow."""

from pathlib import Path
import sqlite3

from ledgerflow.repository import LedgerRepository
from tools.migrate import migrate_to_ledgerflow


def test_migrate_from_v4_exact_reconciliation(tmp_path: Path):
    v4_db = tmp_path / "jzbot.db"
    lf_db = tmp_path / "ledgerflow.db"

    conn = sqlite3.connect(v4_db)
    conn.executescript(
        """
        CREATE TABLE owners (owner_id INTEGER PRIMARY KEY, welcome_sent INTEGER, created_at TEXT, updated_at TEXT);
        CREATE TABLE connections (connection_id TEXT PRIMARY KEY, owner_id INTEGER, is_enabled INTEGER, updated_at TEXT);
        CREATE TABLE customers (
            owner_id INTEGER, chat_id INTEGER, username TEXT, first_name TEXT, last_name TEXT,
            has_wallet INTEGER, balance_tenths INTEGER, deposit_total_tenths INTEGER, withdraw_total_tenths INTEGER,
            entry_count INTEGER, is_disabled INTEGER, created_at TEXT, updated_at TEXT, PRIMARY KEY(owner_id, chat_id)
        );
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, chat_id INTEGER, kind TEXT,
            amount_tenths INTEGER, balance_before INTEGER, balance_after INTEGER,
            source_message_id INTEGER, operator_id INTEGER, created_at TEXT
        );
        CREATE TABLE processed_messages (owner_id INTEGER, chat_id INTEGER, source_message_id INTEGER, command_type TEXT, processed_at TEXT);
        CREATE TABLE receipt_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, chat_id INTEGER, source_message_id INTEGER,
            connection_id TEXT, receipt_type TEXT, receipt_text TEXT, reply_to_message_id INTEGER,
            should_pin INTEGER, status TEXT, attempt_count INTEGER, created_at TEXT,
            next_attempt_at TEXT, delivered_at TEXT, last_error TEXT
        );
        """
    )
    ts = "2026-11-25T10:00:00.000000Z"
    conn.execute("INSERT INTO owners VALUES (100, 1, ?, ?)", (ts, ts))
    conn.execute("INSERT INTO connections VALUES ('L1', 100, 1, ?)", (ts,))
    conn.execute("INSERT INTO customers VALUES (100, 201, 'alice', 'Alice', NULL, 1, 155, 200, 45, 2, 0, ?, ?)", (ts, ts))
    conn.execute("INSERT INTO transactions VALUES (1, 100, 201, 'deposit', 200, 0, 200, 1, 100, ?)", (ts,))
    conn.execute("INSERT INTO transactions VALUES (2, 100, 201, 'withdraw', 45, 200, 155, 2, 100, ?)", (ts,))
    conn.execute("INSERT INTO processed_messages VALUES (100, 201, 1, 'deposit', ?)", (ts,))
    conn.execute("INSERT INTO receipt_outbox VALUES (1, 100, 201, 1, 'L1', 'amount', 'ok', 1, 1, 'delivered', 0, ?, ?, ?, NULL)", (ts, ts, ts))
    conn.commit()
    conn.close()

    summary = migrate_to_ledgerflow(v4_db, lf_db)
    assert summary.source_format == "v4"
    assert summary.ledgers == 1
    assert summary.journal_entries == 2
    assert summary.net_balance_tenths == 155

    repo = LedgerRepository(lf_db)
    try:
        c = repo.get_contact(100, 201)
        assert c.balance_tenths == 155
        txs, cnt = repo.get_journal(100, 201)
        assert cnt == 2
        assert [t.kind for t in txs] == ["out", "in"]
    finally:
        repo.close()
