"""Configuration loader and cross-platform single-instance process lock."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import IO


@dataclass(frozen=True)
class Settings:
    token: str
    db_path: Path
    lock_path: Path
    log_path: Path
    backup_dir: Path
    timezone_offset_hours: int = 8

    @classmethod
    def load(cls) -> Settings:
        file_cfg: dict[str, str] = {}
        cfg_file = os.environ.get("JZBOT_CONFIG", "").strip()
        if cfg_file:
            p = Path(cfg_file).expanduser().resolve()
            if not p.is_file():
                raise RuntimeError(f"Config file not found: {p}")
            if os.name == "posix":
                mode = stat.S_IMODE(p.stat().st_mode)
                if mode & 0o077:
                    raise RuntimeError(f"Config file {p} permissions too open ({oct(mode)}); expected 0600")
            raw = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise RuntimeError("JZBOT_CONFIG must be a JSON object")
            file_cfg = {str(k): str(v) for k, v in raw.items() if v is not None}

        def get(key: str, default: str = "") -> str:
            val = os.environ.get(key)
            if val is not None and val.strip():
                return val.strip()
            return file_cfg.get(key, default).strip()

        token = get("JZBOT_TOKEN")
        if not token:
            raise RuntimeError("Missing required JZBOT_TOKEN")

        db_raw = get("JZBOT_DB_PATH", "jzbot.db")
        db_path = Path(db_raw).expanduser().resolve()
        lock_raw = get("JZBOT_LOCK_PATH")
        lock_path = Path(lock_raw).expanduser().resolve() if lock_raw else db_path.parent / ".jzbot.instance.lock"
        log_raw = get("JZBOT_LOG_PATH")
        log_path = Path(log_raw).expanduser().resolve() if log_raw else db_path.parent / "bot.log"
        backup_raw = get("JZBOT_BACKUP_DIR")
        backup_dir = Path(backup_raw).expanduser().resolve() if backup_raw else db_path.parent / "backups"

        tz_raw = get("JZBOT_TZ_OFFSET", "8")
        try:
            tz_offset = int(tz_raw)
        except ValueError as exc:
            raise RuntimeError(f"Invalid JZBOT_TZ_OFFSET: {tz_raw}") from exc

        return cls(
            token=token,
            db_path=db_path,
            lock_path=lock_path,
            log_path=log_path,
            backup_dir=backup_dir,
            timezone_offset_hours=tz_offset,
        )


class InstanceLock:
    """Advisory OS file lock preventing duplicate bot polling instances."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fp: IO[str] | None = None

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
            raise RuntimeError(f"Another jzbot instance holds lock: {self.path}") from exc
        fp.seek(0)
        fp.truncate()
        fp.write(f"{os.getpid()}\n")
        fp.flush()
        self._fp = fp

    def release(self) -> None:
        if self._fp is None:
            return
        fp = self._fp
        self._fp = None
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

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *args: object) -> None:
        self.release()
