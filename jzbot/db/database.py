"""Connection lifecycle and fail-closed, additive, versioned schema initialization."""

import asyncio
import re
import sqlite3
from pathlib import Path

import aiosqlite

from .legacy_stats import LegacyStatsMigration
from .schema import BASE_SCHEMA, MANAGEMENT_SQL, SCHEMA_VERSION


class SchemaMismatch(RuntimeError):
    pass


def expected_schema():
    connection = sqlite3.connect(":memory:")
    connection.executescript(BASE_SCHEMA)
    for sql in MANAGEMENT_SQL:
        connection.execute(sql)
    result = {
        name: (kind, sql)
        for kind, name, sql in connection.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    }
    connection.close()
    return result


def normalize_sql(sql):
    return re.sub(r"\s+", "", sql)


class Database(LegacyStatsMigration):
    def __init__(self, path, *, require_existing=False):
        self.db_path = Path(path)
        self.require_existing = require_existing
        self.conn = None
        self.write_lock = asyncio.Lock()
        self.delivery_lock = asyncio.Lock()

    def _require_conn(self):
        if self.conn is None:
            raise RuntimeError("Database is not opened")
        return self.conn

    async def _validate_schema(self):
        conn = self._require_conn()
        expected = expected_schema()
        rows = await (
            await conn.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        ).fetchall()
        if self.require_existing and not any(name == "ledger" for _, name, _ in rows):
            raise SchemaMismatch("Existing production database has no ledger table")
        for kind, name, sql in rows:
            if name in expected:
                expected_kind, expected_sql = expected[name]
                if kind != expected_kind or normalize_sql(sql) != normalize_sql(
                    expected_sql
                ):
                    raise SchemaMismatch(f"Incompatible existing schema object: {name}")
            elif kind == "trigger":
                # Unknown triggers may mutate accounting unexpectedly.
                raise SchemaMismatch("Unreviewed database trigger")
        if any(name == "app_meta" for _, name, _ in rows):
            row = await (
                await conn.execute(
                    "SELECT meta_value FROM app_meta WHERE meta_key='jzbot_v2_schema'"
                )
            ).fetchone()
            if row and row[0] != SCHEMA_VERSION:
                raise SchemaMismatch("Unsupported V2 schema version")

    async def open(self):
        if self.conn is not None:
            raise RuntimeError("Database is already open")
        if self.require_existing:
            path = self.db_path.resolve(strict=True)
            if not path.is_file():
                raise ValueError("Production ledger must be an existing regular file")
            # mode=rw guarantees SQLite cannot silently create an empty production DB.
            target = path.as_uri() + "?mode=rw"
        else:
            target = str(self.db_path)
        try:
            self.conn = await aiosqlite.connect(target, uri=self.require_existing)
            self.conn.row_factory = aiosqlite.Row
            await self.conn.execute("PRAGMA busy_timeout=5000")
            await self._validate_schema()
            check = await (await self.conn.execute("PRAGMA quick_check")).fetchall()
            if [tuple(row) for row in check] != [("ok",)]:
                raise SchemaMismatch("SQLite integrity check failed")
            await self.conn.execute("PRAGMA journal_mode=WAL")
            await self.conn.execute("PRAGMA synchronous=NORMAL")
            await self.conn.execute("PRAGMA foreign_keys=ON")
            await self.conn.executescript(BASE_SCHEMA)
            await self.conn.commit()
            marker = await (
                await self.conn.execute(
                    "SELECT meta_value FROM app_meta WHERE meta_key='wallet_stats_v1'"
                )
            ).fetchone()
            if marker is None:
                count = (
                    await (
                        await self.conn.execute("SELECT COUNT(*) FROM wallet_stats")
                    ).fetchone()
                )[0]
                if count:
                    raise SchemaMismatch(
                        "Existing statistics lack migration marker; manual reconciliation required"
                    )
            await self._ensure_wallet_stats_migrated()
            await self._ensure_customer_management_schema()
            await self._validate_schema()
            if await (await self.conn.execute("PRAGMA foreign_key_check")).fetchall():
                raise SchemaMismatch("Foreign key integrity check failed")
        except BaseException:
            await self.close()
            raise

    async def _ensure_customer_management_schema(self):
        conn = self._require_conn()
        async with self.write_lock:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                for sql in MANAGEMENT_SQL:
                    await conn.execute(sql)
                await conn.execute(
                    "INSERT OR IGNORE INTO app_meta(meta_key,meta_value) VALUES('jzbot_v2_schema',?)",
                    (SCHEMA_VERSION,),
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def close(self):
        conn, self.conn = self.conn, None
        if conn is not None:
            try:
                if conn.in_transaction:
                    await conn.rollback()
            finally:
                await conn.close()
