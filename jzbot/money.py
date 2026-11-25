"""Exact 0.1 USDT integer-tenth arithmetic, command parser, and formatters."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

# Max 15 integer digits (999,999,999,999,999.9 USDT) -> safely fits signed 64-bit SQLite INTEGER.
MAX_INTEGER_DIGITS = 15
MAX_SAFE_TENTHS = 9_000_000_000_000_000_000
_AMOUNT_RE = re.compile(r"^([+-])(\d+)(?:\.(\d))?$")

CommandKind = Literal["deposit", "withdraw", "clear", "query"]


@dataclass(frozen=True)
class ParsedCommand:
    kind: CommandKind
    amount_tenths: int = 0  # Always positive for deposit/withdraw, 0 for clear/query


def parse_business_command(text: str | None) -> ParsedCommand | None:
    """Parse a Telegram Business owner message into a bookkeeping command.

    Accepted syntax:
      - `/`        -> query recent 5 entries and current balance
      - `//`       -> clear balance to 0
      - `+10`      -> deposit 10 USDT
      - `-0.5`     -> withdraw 0.5 USDT
    Zero amounts, >1 decimal place, >12 integer digits, or arbitrary text return None.
    """
    if not text:
        return None
    s = text.strip()
    if s == "/":
        return ParsedCommand(kind="query")
    if s == "//":
        return ParsedCommand(kind="clear")

    m = _AMOUNT_RE.match(s)
    if not m:
        return None
    sign, int_part, frac_part = m.groups()
    if len(int_part.lstrip("0") or "0") > MAX_INTEGER_DIGITS:
        return None
    tenths = int(int_part) * 10 + (int(frac_part) if frac_part is not None else 0)
    if tenths <= 0:
        return None
    return ParsedCommand(
        kind="deposit" if sign == "+" else "withdraw",
        amount_tenths=tenths,
    )


def format_tenths(tenths: int) -> str:
    """Format integer tenths into a clean decimal string (e.g. 150 -> '15', -386 -> '-38.6')."""
    neg = tenths < 0
    val = abs(tenths)
    whole, frac = divmod(val, 10)
    prefix = "-" if neg and val != 0 else ""
    if frac == 0:
        return f"{prefix}{whole}"
    return f"{prefix}{whole}.{frac}"


def format_usdt(tenths: int) -> str:
    return f"{format_tenths(tenths)} USDT"


def format_receipt(kind: Literal["deposit", "withdraw", "clear"], amount_tenths: int, balance_after: int) -> str:
    if kind == "deposit":
        return f"入金 {format_usdt(amount_tenths)}, 余额 {format_usdt(balance_after)}"
    if kind == "withdraw":
        return f"出金 {format_usdt(amount_tenths)}, 余额 {format_usdt(balance_after)}"
    return "余额已清零"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def format_local_time(iso_utc: str, tz_offset_hours: int = 8) -> str:
    raw = iso_utc.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local_dt = dt.astimezone(timezone(timedelta(hours=tz_offset_hours)))
        return local_dt.strftime("%m-%d %H:%M")
    except ValueError:
        return iso_utc[:16]


def retry_after_seconds(val: int | float | timedelta) -> float:
    if isinstance(val, timedelta):
        return max(val.total_seconds(), 1.0)
    return max(float(val), 1.0)
