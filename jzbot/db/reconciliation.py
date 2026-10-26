"""Read-only, exact per-customer comparison without emitting identities or rows."""

import hashlib
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Snapshot:
    tables: dict
    customers: dict
    schema: dict

    def public_summary(self):
        return {
            "tables": {name: value[0] for name, value in self.tables.items()},
            "customer_scopes": len(self.customers),
            "digest": hashlib.sha256(
                repr((self.tables, self.customers)).encode()
            ).hexdigest(),
        }


def snapshot(path):
    path = Path(path).resolve(strict=True)
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.execute("BEGIN")
    try:
        if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite integrity check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Foreign key check failed")
        schema = {
            name: (kind, sql)
            for kind, name, sql in conn.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        }
        tables = {}
        scopes = defaultdict(hashlib.sha256)
        for name, (kind, _) in schema.items():
            if kind != "table":
                continue
            columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{name}")')]
            digest = hashlib.sha256()
            count = 0
            quoted = '"' + name.replace('"', '""') + '"'
            for row in conn.execute(f"SELECT * FROM {quoted} ORDER BY rowid"):
                if name == "app_meta" and row[0] == "jzbot_v2_schema":
                    continue  # The only permitted migration delta.
                raw = json.dumps(
                    row, ensure_ascii=False, separators=(",", ":")
                ).encode()
                digest.update(len(raw).to_bytes(8, "big") + raw)
                count += 1
                if "owner_user_id" in columns and "customer_chat_id" in columns:
                    key = (
                        row[columns.index("owner_user_id")],
                        row[columns.index("customer_chat_id")],
                    )
                    scopes[key].update(name.encode() + b"\0" + raw + b"\n")
            tables[name] = (count, digest.hexdigest())
        if "sqlite_sequence" in {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master")
        }:
            rows = conn.execute(
                "SELECT * FROM sqlite_sequence ORDER BY name"
            ).fetchall()
            tables["sqlite_sequence"] = (
                len(rows),
                hashlib.sha256(repr(rows).encode()).hexdigest(),
            )
        return Snapshot(
            tables,
            {key: value.hexdigest() for key, value in sorted(scopes.items())},
            schema,
        )
    finally:
        conn.close()


def assert_preserved(before, after):
    if before.tables != after.tables:
        names = sorted(
            name
            for name in set(before.tables) | set(after.tables)
            if before.tables.get(name) != after.tables.get(name)
        )
        raise AssertionError("Table contents changed: " + ", ".join(names))
    if before.customers != after.customers:
        raise AssertionError("Per-customer reconciliation failed (identities withheld)")
    if before.schema != after.schema:
        raise AssertionError("Existing schema, constraints or indexes changed")
