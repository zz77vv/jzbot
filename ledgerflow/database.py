"""Clean-slate SQLite schema for LedgerFlow (append-only journal + contacts + receipts + announcements)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA_VERSION = "ledgerflow-1.0"

DDL = """
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    owner_id        INTEGER PRIMARY KEY,
    welcomed        INTEGER NOT NULL DEFAULT 0 CHECK(welcomed IN (0, 1)),
    auto_pin        INTEGER NOT NULL DEFAULT 1 CHECK(auto_pin IN (0, 1)),
    daily_report    INTEGER NOT NULL DEFAULT 1 CHECK(daily_report IN (0, 1)),
    last_report_date TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bus_links (
    link_id         TEXT PRIMARY KEY,
    owner_id        INTEGER NOT NULL,
    enabled         INTEGER NOT NULL CHECK(enabled IN (0, 1)),
    updated_at      TEXT NOT NULL,
    FOREIGN KEY(owner_id) REFERENCES accounts(owner_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_bus_links_owner
    ON bus_links(owner_id, enabled DESC, updated_at DESC);

CREATE TABLE IF NOT EXISTS contacts (
    owner_id        INTEGER NOT NULL,
    chat_id         INTEGER NOT NULL,
    alias           TEXT,
    tags            TEXT NOT NULL DEFAULT '',
    tg_username     TEXT,
    tg_first_name   TEXT,
    tg_last_name    TEXT,
    has_ledger      INTEGER NOT NULL DEFAULT 0 CHECK(has_ledger IN (0, 1)),
    balance_tenths  INTEGER NOT NULL DEFAULT 0,
    total_in_tenths INTEGER NOT NULL DEFAULT 0,
    total_out_tenths INTEGER NOT NULL DEFAULT 0,
    tx_count        INTEGER NOT NULL DEFAULT 0,
    archived        INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0, 1)),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY(owner_id, chat_id),
    FOREIGN KEY(owner_id) REFERENCES accounts(owner_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_contacts_balance
    ON contacts(owner_id, has_ledger, balance_tenths DESC);

CREATE INDEX IF NOT EXISTS idx_contacts_updated
    ON contacts(owner_id, has_ledger, updated_at DESC);

CREATE TABLE IF NOT EXISTS journal (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id        INTEGER NOT NULL,
    chat_id         INTEGER NOT NULL,
    kind            TEXT NOT NULL CHECK(kind IN ('in', 'out', 'void', 'settle', 'adjust')),
    delta_tenths    INTEGER NOT NULL,
    amount_tenths   INTEGER NOT NULL,
    balance_before  INTEGER NOT NULL,
    balance_after   INTEGER NOT NULL,
    note            TEXT NOT NULL DEFAULT '',
    ref_entry_id    INTEGER,
    voided_by_id    INTEGER,
    tg_msg_id       INTEGER,
    actor_id        INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    FOREIGN KEY(owner_id, chat_id) REFERENCES contacts(owner_id, chat_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_journal_msg_dedup
    ON journal(owner_id, chat_id, tg_msg_id)
    WHERE tg_msg_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_journal_contact
    ON journal(owner_id, chat_id, id DESC);

CREATE INDEX IF NOT EXISTS idx_journal_owner_time
    ON journal(owner_id, created_at DESC);

CREATE TABLE IF NOT EXISTS handled_msgs (
    owner_id        INTEGER NOT NULL,
    chat_id         INTEGER NOT NULL,
    tg_msg_id       INTEGER NOT NULL,
    intent          TEXT NOT NULL,
    handled_at      TEXT NOT NULL,
    PRIMARY KEY(owner_id, chat_id, tg_msg_id)
);

CREATE TABLE IF NOT EXISTS receipts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id        INTEGER NOT NULL,
    chat_id         INTEGER NOT NULL,
    tg_msg_id       INTEGER NOT NULL,
    link_id         TEXT NOT NULL,
    html_text       TEXT NOT NULL,
    reply_msg_id    INTEGER,
    should_pin      INTEGER NOT NULL DEFAULT 1 CHECK(should_pin IN (0, 1)),
    status          TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'sent')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_try_at     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    sent_at         TEXT,
    last_error      TEXT,
    UNIQUE(owner_id, chat_id, tg_msg_id),
    FOREIGN KEY(owner_id) REFERENCES accounts(owner_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_receipts_due
    ON receipts(status, next_try_at, id);

CREATE TABLE IF NOT EXISTS announcements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id        INTEGER NOT NULL,
    target_desc     TEXT NOT NULL,
    body_text       TEXT NOT NULL,
    status          TEXT NOT NULL CHECK(status IN ('running', 'paused', 'done', 'cancelled')),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    FOREIGN KEY(owner_id) REFERENCES accounts(owner_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_announcements_owner
    ON announcements(owner_id, id DESC);

CREATE TABLE IF NOT EXISTS announcement_deliveries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ann_id          INTEGER NOT NULL,
    chat_id         INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK(status IN ('pending', 'sending', 'sent', 'failed', 'uncertain', 'cancelled')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_try_at     TEXT NOT NULL,
    tg_sent_msg_id  INTEGER,
    error_text      TEXT,
    updated_at      TEXT NOT NULL,
    UNIQUE(ann_id, chat_id),
    FOREIGN KEY(ann_id) REFERENCES announcements(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ann_deliveries_due
    ON announcement_deliveries(ann_id, status, next_try_at, id);

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id        INTEGER NOT NULL,
    action          TEXT NOT NULL,
    target_chat_id  INTEGER,
    detail          TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);
"""


def open_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA cache_size = -16000")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA mmap_size = 67108864")
    conn.executescript(DDL)
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
    if "daily_report" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN daily_report INTEGER NOT NULL DEFAULT 1 CHECK(daily_report IN (0, 1))")
    if "last_report_date" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN last_report_date TEXT NOT NULL DEFAULT ''")
    conn.execute("INSERT OR IGNORE INTO meta(k, v) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return conn
