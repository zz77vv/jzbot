"""End-to-end test for V2 ledger.db -> redesigned jzbot.db migration tool."""

import sqlite3
from pathlib import Path

import pytest

from jzbot.store import Store
from tools.migrate_v2 import migrate_v2_to_new

V2_SCHEMA_SQL = """
CREATE TABLE business_accounts (
    owner_user_id INTEGER PRIMARY KEY,
    welcome_sent INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE business_connections (
    connection_id TEXT PRIMARY KEY,
    owner_user_id INTEGER NOT NULL,
    is_enabled INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE customer_profiles (
    owner_user_id INTEGER NOT NULL,
    customer_chat_id INTEGER NOT NULL,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(owner_user_id, customer_chat_id)
);
CREATE TABLE wallets (
    owner_user_id INTEGER NOT NULL,
    customer_chat_id INTEGER NOT NULL,
    balance_tenths TEXT NOT NULL DEFAULT '0',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(owner_user_id, customer_chat_id)
);
CREATE TABLE ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    customer_chat_id INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    entry_type TEXT NOT NULL,
    amount_tenths TEXT NOT NULL,
    balance_before TEXT NOT NULL,
    balance_after TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);
CREATE TABLE processed_messages (
    owner_user_id INTEGER NOT NULL,
    customer_chat_id INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    command_type TEXT NOT NULL,
    processed_at_utc TEXT NOT NULL,
    PRIMARY KEY(owner_user_id, customer_chat_id, source_message_id)
);
CREATE TABLE wallet_stats (
    owner_user_id INTEGER NOT NULL,
    customer_chat_id INTEGER NOT NULL,
    deposit_total_tenths TEXT NOT NULL DEFAULT '0',
    withdraw_total_tenths TEXT NOT NULL DEFAULT '0',
    entry_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(owner_user_id, customer_chat_id)
);
CREATE TABLE receipt_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    customer_chat_id INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    business_connection_id TEXT NOT NULL,
    receipt_type TEXT NOT NULL,
    receipt_text TEXT NOT NULL,
    reply_to_message_id INTEGER,
    should_pin INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at_utc TEXT NOT NULL,
    next_attempt_at_utc TEXT NOT NULL,
    delivered_at_utc TEXT,
    last_error TEXT
);
CREATE TABLE customer_management (
    owner_user_id INTEGER NOT NULL,
    customer_chat_id INTEGER NOT NULL,
    is_disabled INTEGER NOT NULL,
    updated_by INTEGER NOT NULL,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY(owner_user_id, customer_chat_id)
);
CREATE TABLE admin_operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL UNIQUE,
    owner_user_id INTEGER NOT NULL,
    administrator_id INTEGER NOT NULL,
    customer_chat_id INTEGER NOT NULL,
    operation_type TEXT NOT NULL DEFAULT 'admin_zero',
    entry_type TEXT NOT NULL DEFAULT 'clear',
    expected_balance TEXT NOT NULL,
    expected_ledger_id INTEGER NOT NULL,
    expected_admin_id INTEGER NOT NULL,
    expected_entry_count INTEGER NOT NULL,
    balance_before TEXT,
    balance_after TEXT,
    result TEXT NOT NULL DEFAULT 'pending',
    created_at_utc TEXT NOT NULL,
    expires_at_utc TEXT NOT NULL,
    completed_at_utc TEXT,
    ledger_anchor_id INTEGER,
    back_kind TEXT NOT NULL,
    back_page INTEGER NOT NULL
);
"""


def test_migrate_v2_to_new_full_reconciliation(tmp_path: Path):
    src = tmp_path / "old_ledger.db"
    dst = tmp_path / "new_jzbot.db"

    conn = sqlite3.connect(src)
    conn.executescript(V2_SCHEMA_SQL)
    ts = "2026-11-01T10:00:00.000000Z"
    conn.execute("INSERT INTO business_accounts VALUES (100, 1, ?, ?)", (ts, ts))
    conn.execute("INSERT INTO business_connections VALUES ('conn-1', 100, 1, ?)", (ts,))
    conn.execute("INSERT INTO customer_profiles VALUES (100, 201, 'alice', 'Alice', NULL, ?)", (ts,))
    conn.execute("INSERT INTO customer_profiles VALUES (100, 202, 'bob', 'Bob', NULL, ?)", (ts,))
    conn.execute("INSERT INTO customer_profiles VALUES (100, 203, 'nobal', 'NoBal', NULL, ?)", (ts,))
    conn.execute("INSERT INTO wallets VALUES (100, 201, '155', ?, ?)", (ts, ts))
    conn.execute("INSERT INTO wallets VALUES (100, 202, '0', ?, ?)", (ts, ts))
    conn.execute("INSERT INTO wallet_stats VALUES (100, 201, '200', '45', 2, ?)", (ts,))
    conn.execute("INSERT INTO wallet_stats VALUES (100, 202, '100', '0', 1, ?)", (ts,))
    conn.execute("INSERT INTO customer_management VALUES (100, 202, 1, 100, ?)", (ts,))
    conn.execute("INSERT INTO ledger(owner_user_id, customer_chat_id, source_message_id, entry_type, amount_tenths, balance_before, balance_after, created_at_utc) VALUES (100, 201, 1, 'deposit', '200', '0', '200', ?)", (ts,))
    conn.execute("INSERT INTO ledger(owner_user_id, customer_chat_id, source_message_id, entry_type, amount_tenths, balance_before, balance_after, created_at_utc) VALUES (100, 201, 2, 'withdraw', '45', '200', '155', ?)", (ts,))
    conn.execute("INSERT INTO ledger(owner_user_id, customer_chat_id, source_message_id, entry_type, amount_tenths, balance_before, balance_after, created_at_utc) VALUES (100, 202, 3, 'deposit', '100', '0', '100', ?)", (ts,))
    conn.execute(
        "INSERT INTO admin_operations(operation_id, owner_user_id, administrator_id, customer_chat_id, expected_balance, expected_ledger_id, expected_admin_id, expected_entry_count, balance_before, balance_after, result, created_at_utc, expires_at_utc, completed_at_utc, ledger_anchor_id, back_kind, back_page) VALUES ('op1', 100, 100, 202, '100', 3, 0, 1, '100', '0', 'applied', ?, ?, ?, 3, 'a', 0)",
        (ts, ts, ts),
    )
    conn.execute("INSERT INTO processed_messages VALUES (100, 201, 1, 'deposit', ?)", (ts,))
    conn.execute("INSERT INTO receipt_outbox(owner_user_id, customer_chat_id, source_message_id, business_connection_id, receipt_type, receipt_text, reply_to_message_id, should_pin, status, attempt_count, created_at_utc, next_attempt_at_utc, delivered_at_utc, last_error) VALUES (100, 201, 1, 'conn-1', 'amount', '入金 20 USDT, 余额 20 USDT', 1, 1, 'delivered', 0, ?, ?, ?, NULL)", (ts, ts, ts))
    conn.commit()
    conn.close()

    report = migrate_v2_to_new(src, dst)
    assert report.owners == 1
    assert report.connections == 1
    assert report.customers_total == 3
    assert report.wallets == 2
    assert report.transactions == 4  # 3 ledger + 1 applied admin_clear
    assert report.total_balance_tenths == 155

    # Refuses to overwrite existing target
    with pytest.raises(FileExistsError):
        migrate_v2_to_new(src, dst)

    store = Store(dst)
    try:
        alice = store.get_customer(100, 201)
        assert alice.balance_tenths == 155
        assert alice.deposit_total_tenths == 200
        assert alice.withdraw_total_tenths == 45

        bob = store.get_customer(100, 202)
        assert bob.balance_tenths == 0
        assert bob.is_disabled is True
        assert bob.entry_count == 2
        txs, cnt = store.get_recent_transactions(100, 202, limit=10)
        assert cnt == 2
        assert [t.kind for t in txs] == ["admin_clear", "deposit"]
        assert txs[0].source_message_id is None

        nobal = store.get_customer(100, 203)
        assert nobal.has_wallet is False
    finally:
        store.close()
