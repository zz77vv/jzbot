"""Independent durable ingress; this database never contains financial tables."""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS trusted_commands(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 owner INTEGER NOT NULL, customer INTEGER NOT NULL, source INTEGER NOT NULL,
 connection TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('amount','clear')),
 delta TEXT, status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','done')),
 attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0,
 error_type TEXT, notice TEXT CHECK(notice IN ('deferred','recovered')),
 notice_due REAL NOT NULL DEFAULT 0, deferred_notified INTEGER NOT NULL DEFAULT 0,
 UNIQUE(owner,customer,source)
);
CREATE INDEX IF NOT EXISTS commands_due ON trusted_commands(status,due,id);
CREATE INDEX IF NOT EXISTS commands_scope ON trusted_commands(owner,customer,status,id);
CREATE TABLE IF NOT EXISTS command_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
"""


class CommandRepository:
    def __init__(self, path, ledger_path):
        self.path = Path(path)
        self.ledger_path = str(Path(ledger_path).resolve())
        self.lock = asyncio.Lock()
        self.initialized = False

    async def open(self):
        async with self.lock:
            if self.initialized:
                return
            if self.path.is_symlink():
                raise ValueError("Command database cannot be a symlink")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                os.close(fd)
            async with aiosqlite.connect(self.path) as conn:
                names = {r[0] for r in await (await conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )).fetchall()}
                if names - {"trusted_commands", "command_meta", "sqlite_sequence"}:
                    raise ValueError("Unexpected tables in command database; no initialization")
                if "trusted_commands" in names:
                    binding = None
                    if "command_meta" in names:
                        binding = await (await conn.execute(
                            "SELECT value FROM command_meta WHERE key='ledger_path'"
                        )).fetchone()
                    if binding is None or binding[0] != self.ledger_path:
                        raise ValueError("Command queue belongs to another ledger or lacks identity metadata")
                if os.name != "nt":
                    self.path.chmod(0o600)
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA synchronous=FULL")
                await conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
                await conn.execute(
                    "INSERT OR IGNORE INTO command_meta VALUES('ledger_path',?)",
                    (self.ledger_path,),
                )
                await conn.commit()
            self.initialized = True

    async def close(self):
        # Each operation owns a short-lived connection, including in tests that
        # construct Runtime without running the Application lifecycle.
        async with self.lock:
            self.initialized = False

    @asynccontextmanager
    async def transaction(self):
        await self.open()
        async with self.lock:
            async with aiosqlite.connect(self.path) as conn:
                conn.row_factory = aiosqlite.Row
                await conn.execute("PRAGMA busy_timeout=5000")
                await conn.execute("PRAGMA synchronous=FULL")
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                    await conn.commit()
                except BaseException:
                    await conn.rollback()
                    raise

    async def enqueue(self, owner, customer, source, connection, kind, delta):
        async with self.transaction() as conn:
            await conn.execute(
                """INSERT OR IGNORE INTO trusted_commands
                (owner,customer,source,connection,kind,delta) VALUES(?,?,?,?,?,?)""",
                (owner, customer, source, connection, kind,
                 str(delta) if delta is not None else None),
            )
            return dict(await (await conn.execute(
                "SELECT * FROM trusted_commands WHERE owner=? AND customer=? AND source=?",
                (owner, customer, source),
            )).fetchone())

    async def get(self, owner, customer, source):
        async with self.transaction() as conn:
            row = await (await conn.execute(
                "SELECT * FROM trusted_commands WHERE owner=? AND customer=? AND source=?",
                (owner, customer, source),
            )).fetchone()
            return dict(row) if row else None

    async def next_due(self, now, scope=None):
        async with self.transaction() as conn:
            clause = " AND c.owner=? AND c.customer=?" if scope else ""
            params = (now, *scope) if scope else (now,)
            row = await (await conn.execute(
                """SELECT c.* FROM trusted_commands c
                WHERE c.status='pending' AND c.due<=? AND NOT EXISTS (
                    SELECT 1 FROM trusted_commands earlier
                    WHERE earlier.owner=c.owner AND earlier.customer=c.customer
                    AND earlier.status='pending' AND earlier.id<c.id)
                """ + clause + " ORDER BY c.due,c.id LIMIT 1", params,
            )).fetchone()
            return dict(row) if row else None

    async def defer(self, row):
        async with self.transaction() as conn:
            await conn.execute(
                """UPDATE trusted_commands SET notice='deferred'
                WHERE id=? AND status='pending' AND deferred_notified=0""", (row["id"],),
            )

    async def failed(self, row, now, error_type):
        attempts = row["attempts"] + 1
        delay = min(300, 2 ** min(attempts, 9))
        async with self.transaction() as conn:
            await conn.execute(
                """UPDATE trusted_commands SET attempts=?,due=?,error_type=?,
                notice=CASE WHEN deferred_notified=0 THEN 'deferred' ELSE notice END
                WHERE id=? AND status='pending'""",
                (attempts, now + delay, error_type, row["id"]),
            )

    async def complete(self, row):
        async with self.transaction() as conn:
            await conn.execute(
                """UPDATE trusted_commands SET status='done',error_type=NULL,
                notice=CASE WHEN deferred_notified=1 OR notice='deferred'
                    THEN 'recovered' ELSE NULL END,
                notice_due=0 WHERE id=?""", (row["id"],),
            )

    async def notices(self, now):
        async with self.transaction() as conn:
            return [dict(r) for r in await (await conn.execute(
                """SELECT * FROM trusted_commands WHERE notice IS NOT NULL
                AND notice_due<=? ORDER BY id LIMIT 10""", (now,),
            )).fetchall()]

    async def finish_notice(self, row, now, *, sent):
        async with self.transaction() as conn:
            if sent:
                await conn.execute(
                    """UPDATE trusted_commands SET notice=NULL,
                    deferred_notified=CASE WHEN ?='deferred' THEN 1 ELSE deferred_notified END
                    WHERE id=? AND notice=?""", (row["notice"], row["id"], row["notice"]),
                )
            else:
                await conn.execute(
                    "UPDATE trusted_commands SET notice_due=? WHERE id=? AND notice=?",
                    (now + 30, row["id"], row["notice"]),
                )
