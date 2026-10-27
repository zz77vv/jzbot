"""Durable broadcast queue in a separate database; never changes ledger data."""

import asyncio
import os
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS broadcast_jobs(
 id TEXT PRIMARY KEY, owner INTEGER NOT NULL, body TEXT NOT NULL,
 audience TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('running','paused','cancelled','completed')),
 created REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_broadcast ON broadcast_jobs(owner)
 WHERE status IN ('running','paused');
CREATE TABLE IF NOT EXISTS broadcast_recipients(
 job_id TEXT NOT NULL REFERENCES broadcast_jobs(id), customer INTEGER NOT NULL,
 name TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','sending','sent','failed','uncertain','cancelled')),
 attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0,
 category TEXT, connection TEXT, message_id INTEGER,
 PRIMARY KEY(job_id,customer)
);
CREATE INDEX IF NOT EXISTS broadcast_due ON broadcast_recipients(status,due,job_id);
CREATE TABLE IF NOT EXISTS broadcast_meta(key TEXT PRIMARY KEY,value REAL NOT NULL);
"""


class BroadcastRepository:
    def __init__(self, path):
        self.path = Path(path)
        self.conn = None
        self.lock = asyncio.Lock()

    async def open(self):
        if self.conn is not None:
            return
        if self.path.is_symlink():
            raise ValueError("Broadcast database cannot be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Refuse any existing unrelated/financial database before initializing it.
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        try:
            names = {r[0] for r in await (await self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )).fetchall()}
            if names - {"broadcast_jobs", "broadcast_recipients", "broadcast_meta"}:
                raise ValueError("Unexpected tables in broadcast database; no initialization")
            if os.name != "nt":
                self.path.chmod(0o600)
            await self.conn.execute("PRAGMA foreign_keys=ON")
            await self.conn.execute("PRAGMA journal_mode=WAL")
            await self.conn.execute("PRAGMA busy_timeout=5000")
            await self.conn.executescript(SCHEMA)
            # Never auto-resume a campaign after a restart. A sending row may
            # already have reached Telegram; it must not be replayed automatically.
            await self.conn.execute("UPDATE broadcast_recipients SET status='uncertain',category='restart_uncertain' WHERE status='sending'")
            await self.conn.execute("UPDATE broadcast_jobs SET status='paused' WHERE status='running'")
            await self.conn.commit()
        except BaseException:
            await self.close()
            raise

    async def close(self):
        conn, self.conn = self.conn, None
        if conn is not None:
            await conn.close()

    def require(self):
        if self.conn is None:
            raise RuntimeError("Broadcast queue is not opened")
        return self.conn

    async def create(self, job, owner, body, audience, recipients, now):
        conn = self.require()
        async with self.lock:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await (await conn.execute(
                    "SELECT id FROM broadcast_jobs WHERE id=? AND owner=?", (job, owner)
                )).fetchone()
                if existing:
                    await conn.rollback()
                    return False
                await conn.execute("INSERT INTO broadcast_jobs VALUES(?,?,?,?, 'running',?)",
                                   (job, owner, body, audience, now))
                await conn.executemany(
                    "INSERT INTO broadcast_recipients(job_id,customer,name,status) VALUES(?,?,?,'pending')",
                    [(job, r['customer_chat_id'], r['name']) for r in recipients],
                )
                await conn.commit()
                return True
            except BaseException:
                await conn.rollback()
                raise

    async def list_jobs(self, owner, page=0):
        conn = self.require()
        rows = await (await conn.execute(
            "SELECT * FROM broadcast_jobs WHERE owner=? ORDER BY created DESC,id DESC LIMIT 10 OFFSET ?",
            (owner, max(0, page) * 10),
        )).fetchall()
        total = (await (await conn.execute("SELECT COUNT(*) FROM broadcast_jobs WHERE owner=?", (owner,))).fetchone())[0]
        return rows, total

    async def get(self, owner, job):
        conn = self.require()
        async with self.lock:
            row = await (await conn.execute("SELECT * FROM broadcast_jobs WHERE id=? AND owner=?", (job, owner))).fetchone()
            if row is None:
                return None
            counts = {r[0]: r[1] for r in await (await conn.execute(
                "SELECT status,COUNT(*) FROM broadcast_recipients WHERE job_id=? GROUP BY status", (job,)
            )).fetchall()}
        return dict(row) | {"counts": counts, "total": sum(counts.values())}

    async def recipient_page(self, owner, job, page):
        if await self.get(owner, job) is None:
            return [], 0
        conn = self.require()
        total = (await (await conn.execute("SELECT COUNT(*) FROM broadcast_recipients WHERE job_id=?", (job,))).fetchone())[0]
        rows = await (await conn.execute(
            "SELECT * FROM broadcast_recipients WHERE job_id=? ORDER BY customer LIMIT 10 OFFSET ?",
            (job, page * 10),
        )).fetchall()
        return rows, total

    async def control(self, owner, job, action):
        conn = self.require()
        targets = {'pause': ('paused', 'running'), 'resume': ('running', 'paused'),
                   'cancel': ('cancelled', None)}
        target, required = targets[action]
        async with self.lock:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                row = await (await conn.execute("SELECT status FROM broadcast_jobs WHERE id=? AND owner=?", (job, owner))).fetchone()
                if row is None or row[0] not in ('running', 'paused') or (required and row[0] != required):
                    await conn.rollback()
                    return False
                if action == 'resume':
                    outstanding = (await (await conn.execute(
                        "SELECT COUNT(*) FROM broadcast_recipients WHERE job_id=? AND status IN ('pending','sending')", (job,)
                    )).fetchone())[0]
                    if not outstanding:
                        target = 'completed'
                await conn.execute("UPDATE broadcast_jobs SET status=? WHERE id=? AND owner=?", (target, job, owner))
                if action == 'cancel':
                    await conn.execute("UPDATE broadcast_recipients SET status='cancelled' WHERE job_id=? AND status='pending'", (job,))
                await conn.commit()
                return True
            except BaseException:
                await conn.rollback()
                raise

    async def claim(self, now):
        conn = self.require()
        async with self.lock:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cooldown = await (await conn.execute("SELECT value FROM broadcast_meta WHERE key='cooldown'")).fetchone()
                if cooldown and cooldown[0] > now:
                    await conn.rollback()
                    return None
                row = await (await conn.execute(
                    """SELECT r.*,j.owner,j.body FROM broadcast_recipients r
                    JOIN broadcast_jobs j ON j.id=r.job_id
                    WHERE j.status='running' AND r.status='pending' AND r.due<=?
                    ORDER BY r.due,j.created,r.customer LIMIT 1""", (now,)
                )).fetchone()
                if row is None:
                    await conn.rollback()
                    return None
                await conn.execute("UPDATE broadcast_recipients SET status='sending',attempts=attempts+1 WHERE job_id=? AND customer=?", (row['job_id'], row['customer']))
                await conn.commit()
                return dict(row) | {'attempts': row['attempts'] + 1}
            except BaseException:
                await conn.rollback()
                raise

    async def finish(self, row, status, category=None, *, due=0, connection=None, message_id=None, cooldown=0):
        conn = self.require()
        async with self.lock:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                job_status = (await (await conn.execute("SELECT status FROM broadcast_jobs WHERE id=?", (row['job_id'],))).fetchone())[0]
                if status == 'pending' and job_status == 'cancelled':
                    status = 'cancelled'
                await conn.execute(
                    "UPDATE broadcast_recipients SET status=?,category=?,due=?,connection=?,message_id=? WHERE job_id=? AND customer=? AND status='sending'",
                    (status, category, due, connection, message_id, row['job_id'], row['customer']),
                )
                if cooldown:
                    await conn.execute("INSERT INTO broadcast_meta VALUES('cooldown',?) ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)", (cooldown,))
                await conn.execute("""UPDATE broadcast_jobs SET status='completed'
                    WHERE id=? AND status IN ('running','paused') AND NOT EXISTS(
                    SELECT 1 FROM broadcast_recipients WHERE job_id=? AND status IN ('pending','sending'))""",
                    (row['job_id'], row['job_id']))
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
