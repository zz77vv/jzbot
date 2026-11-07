"""Explicit runtime configuration; never read secrets at import time."""

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    db_path: Path
    lock_path: Path
    log_path: Path
    require_existing_db: bool = True
    release: str = "development"

    @property
    def broadcast_path(self):
        """Separate runtime data, beside this instance's log, never the ledger."""
        path = self.log_path.parent / "broadcast.db"
        if path.resolve() in {self.db_path.resolve(), self.log_path.resolve(), self.lock_path.resolve()}:
            raise ValueError("Broadcast database path conflicts with another runtime file")
        return path

    @property
    def command_path(self):
        """Trusted command recovery has its own sidecar, never a ledger table."""
        path = self.log_path.parent / "commands.db"
        if path.resolve() in {
            self.db_path.resolve(), self.log_path.resolve(), self.lock_path.resolve(),
            self.broadcast_path.resolve(),
        }:
            raise ValueError("Command database path conflicts with another runtime file")
        return path

    @classmethod
    def from_environment(cls):
        values = {}
        filename = os.environ.get("JZBOT_CONFIG")
        if filename:
            path = Path(filename).resolve(strict=True)
            info = path.stat()
            if os.name != "nt" and (
                stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.getuid()
            ):
                raise ValueError(
                    "External config must be owned by this user and mode 0600"
                )
            values = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(values, dict):
                raise ValueError("External config must be an object")

        def value(name, default=None):
            return os.environ.get(name, values.get(name, default))

        token = value("JZBOT_TOKEN", "")
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("JZBOT_TOKEN is missing or invalid (value withheld)")
        raw_path = value("JZBOT_DB_PATH")
        if not raw_path or not Path(raw_path).is_absolute():
            raise ValueError(
                "JZBOT_DB_PATH must explicitly name an absolute existing ledger"
            )
        db_path = Path(raw_path).resolve(strict=True)
        if not db_path.is_file():
            raise ValueError("Ledger is not a regular file")
        lock = Path(
            value("JZBOT_LOCK_PATH", str(db_path.parent / ".jzbot.instance.lock"))
        ).resolve()
        log = Path(value("JZBOT_LOG_PATH", str(Path.cwd() / "bot.log"))).resolve()
        if lock == db_path or log == db_path or lock == log:
            raise ValueError("Database, lock and log paths must be distinct")
        return cls(
            token, db_path, lock, log, True, value("JZBOT_RELEASE", "unversioned")
        )

