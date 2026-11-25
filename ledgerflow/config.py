"""Configuration loader and single-instance OS file lock for LedgerFlow."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import IO


@dataclass(frozen=True)
class AppConfig:
    bot_token: str
    db_path: Path
    lock_path: Path
    log_path: Path
    backup_dir: Path
    tz_offset_hours: int = 8
    currency_unit: str = "USDT"

    @classmethod
    def load(cls) -> AppConfig:
        file_data: dict[str, str] = {}
        cfg_file = os.environ.get("LEDGERFLOW_CONFIG", "").strip()
        if cfg_file:
            p = Path(cfg_file).expanduser().resolve()
            if not p.is_file():
                raise RuntimeError(f"Config file not found: {p}")
            if os.name == "posix":
                mode = stat.S_IMODE(p.stat().st_mode)
                if mode & 0o077:
                    raise RuntimeError(f"Config file {p} must have 0600 permissions (got {oct(mode)})")
            raw = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise RuntimeError("LEDGERFLOW_CONFIG must be a JSON object")
            file_data = {str(k): str(v) for k, v in raw.items() if v is not None}

        def read_key(key: str, default: str = "") -> str:
            env_val = os.environ.get(key)
            if env_val is not None and env_val.strip():
                return env_val.strip()
            return file_data.get(key, default).strip()

        token = read_key("LEDGERFLOW_TOKEN")
        if not token:
            raise RuntimeError("Missing required LEDGERFLOW_TOKEN")

        db_path = Path(read_key("LEDGERFLOW_DB_PATH", "ledgerflow.db")).expanduser().resolve()
        lock_raw = read_key("LEDGERFLOW_LOCK_PATH")
        lock_path = Path(lock_raw).expanduser().resolve() if lock_raw else db_path.parent / ".ledgerflow.lock"
        log_raw = read_key("LEDGERFLOW_LOG_PATH")
        log_path = Path(log_raw).expanduser().resolve() if log_raw else db_path.parent / "ledgerflow.log"
        backup_raw = read_key("LEDGERFLOW_BACKUP_DIR")
        backup_dir = Path(backup_raw).expanduser().resolve() if backup_raw else db_path.parent / "backups"
        tz_offset = int(read_key("LEDGERFLOW_TZ_OFFSET", "8"))
        currency = read_key("LEDGERFLOW_CURRENCY", "USDT") or "USDT"

        return cls(
            bot_token=token,
            db_path=db_path,
            lock_path=lock_path,
            log_path=log_path,
            backup_dir=backup_dir,
            tz_offset_hours=tz_offset,
            currency_unit=currency,
        )


class ProcessLock:
    """Non-blocking exclusive OS file lock preventing duplicate polling."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: IO[str] | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fp = open(self.path, "a+", encoding="utf-8")
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt

                fp.seek(0)
                msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            fp.close()
            raise RuntimeError(f"Another LedgerFlow instance holds lock: {self.path}") from exc
        fp.seek(0)
        fp.truncate()
        fp.write(f"{os.getpid()}\n")
        fp.flush()
        self._handle = fp

    def release(self) -> None:
        if self._handle is None:
            return
        fp = self._handle
        self._handle = None
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
            else:
                import msvcrt

                fp.seek(0)
                msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            fp.close()

    def __enter__(self) -> ProcessLock:
        self.acquire()
        return self

    def __exit__(self, *args: object) -> None:
        self.release()
