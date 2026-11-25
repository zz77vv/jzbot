"""Clean-slate unified SQLite schema for JZBot."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA_VERSION = "4.0"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    meta_key        TEXT PRIMARY KEY,
    meta_value      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS owners (
    owner_id        INTEGER PRIMARY KEY,
    welcome_sent    INTEGER NOT NULL DEFAULT 0 CHECK(welcome_sent IN (0, 1)),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connections (
    connection_id   TEXT PRIMARY KEY,
    owner_id        INTEGER NOT NULL,
    is_enabled      INTEGER NOT NULL CHECK(is_enabled IN (0, 1)),
    updated_at      TEXT NOT NULL,
    FOREIGN KEY(owner_id) REFERENCES owners(owner_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_connections_owner
    ON connections(owner_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS customers (
    owner_id                INTEGER NOT NULL,
    chat_id                 INTEGER NOT NULL,
    username                TEXT,
    first_name              TEXT,
    last_name               TEXT,
    has_wallet              INTEGER NOT NULL DEFAULT 0 CHECK(has_wallet IN (0, 1)),
    balance_tenths          INTEGER NOT NULL DEFAULT 0,
    deposit_total_tenths    INTEGER NOT NULL DEFAULT 0,
    withdraw_total_tenths   INTEGER NOT NULL DEFAULT 0,
    entry_count             INTEGER NOT NULL DEFAULT 0,
    is_disabled             INTEGER NOT NULL DEFAULT 0 CHECK(is_disabled IN (0, 1)),
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    PRIMARY KEY(owner_id, chat_id),
    FOREIGN KEY(owner_id) REFERENCES owners(owner_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_customers_owner_updated
    ON customers(owner_id, has_wallet, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_customers_owner_balance
    ON customers(owner_id, has_wallet, balance_tenths DESC);

CREATE TABLE IF NOT EXISTS transactions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id            INTEGER NOT NULL,
    chat_id             INTEGER NOT NULL,
    kind                TEXT NOT NULL CHECK(kind IN ('deposit', 'withdraw', 'clear', 'admin_clear')),
    amount_tenths       INTEGER NOT NULL,
    balance_before      INTEGER NOT NULL,
    balance_after       INTEGER NOT NULL,
    source_message_id   INTEGER,
    operator_id         INTEGER NOT NULL,
    created_at          TEXT NOT NULL,
    FOREIGN KEY(owner_id, chat_id) REFERENCES customers(owner_id, chat_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_source_dedup
    ON transactions(owner_id, chat_id, source_message_id)
    WHERE source_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_transactions_history
    ON transactions(owner_id, chat_id, id DESC);

CREATE TABLE IF NOT EXISTS processed_messages (
    owner_id            INTEGER NOT NULL,
    chat_id             INTEGER NOT NULL,
    source_message_id   INTEGER NOT NULL,
    command_type        TEXT NOT NULL,
    processed_at        TEXT NOT NULL,
    PRIMARY KEY(owner_id, chat_id, source_message_id)
);

CREATE TABLE IF NOT EXISTS receipt_outbox (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id            INTEGER NOT NULL,
    chat_id             INTEGER NOT NULL,
    source_message_id   INTEGER NOT NULL,
    connection_id       TEXT NOT NULL,
    receipt_type        TEXT NOT NULL CHECK(receipt_type IN ('amount', 'clear')),
    receipt_text        TEXT NOT NULL,
    reply_to_message_id INTEGER,
    should_pin          INTEGER NOT NULL DEFAULT 1 CHECK(should_pin IN (0, 1)),
    status              TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'delivered')),
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    next_attempt_at     TEXT NOT NULL,
    delivered_at        TEXT,
    last_error          TEXT,
    UNIQUE(owner_id, chat_id, source_message_id, receipt_type),
    FOREIGN KEY(owner_id) REFERENCES owners(owner_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_receipt_outbox_due
    ON receipt_outbox(status, next_attempt_at, id);

CREATE TABLE IF NOT EXISTS broadcast_jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id            INTEGER NOT NULL,
    scopes_json         TEXT NOT NULL,
    message_text        TEXT NOT NULL,
    status              TEXT NOT NULL CHECK(status IN ('running', 'paused', 'completed', 'cancelled')),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY(owner_id) REFERENCES owners(owner_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_broadcast_jobs_owner
    ON broadcast_jobs(owner_id, id DESC);

CREATE TABLE IF NOT EXISTS broadcast_recipients (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id              INTEGER NOT NULL,
    chat_id             INTEGER NOT NULL,
    status              TEXT NOT NULL CHECK(status IN ('pending', 'sending', 'sent', 'failed', 'uncertain', 'cancelled')),
    attempts            INTEGER NOT NULL DEFAULT 0,
    next_attempt_at     TEXT NOT NULL,
    message_id          INTEGER,
    error_reason        TEXT,
    updated_at          TEXT NOT NULL,
    UNIQUE(job_id, chat_id),
    FOREIGN KEY(job_id) REFERENCES broadcast_jobs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_broadcast_recipients_due
    ON broadcast_recipients(job_id, status, next_attempt_at, id);
"""


def connect_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(meta_key, meta_value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return conn
