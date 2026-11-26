"""Core domain types, exact 0.1-unit integer arithmetic, command parser, and HTML formatters."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from ledgerflow.emojis import DOWN, OK, REFRESH, UP, tg_emoji

# 1 internal unit = 0.1 USDT. Max 15 integer digits safely fits SQLite signed INT64.
MAX_INT_DIGITS = 15
MAX_SAFE_TENTHS = 9_000_000_000_000_000_000
MAX_NOTE_LEN = 80

# Supports:
#   +100           -> deposit 100
#   +100.5 货款     -> deposit 100.5 with note "货款"
#   +100货款        -> deposit 100 with note "货款"
#   +100.50        -> deposit 100.5
#   -20.5 结账      -> withdraw 20.5 with note "结账"
# Requires whitespace or Chinese char before optional note so "+10USDT" or "+1.23" are never misparsed!
_TX_RE = re.compile(r"^([+-])(\d+)(?:\.(\d)0?)?(?:(?:\s+|(?=[\u4e00-\u9fff]))(.+))?$", re.DOTALL)

IntentKind = Literal["in", "out", "undo", "query", "settle"]


@dataclass(frozen=True)
class ParsedIntent:
    kind: IntentKind
    amount_tenths: int = 0
    note: str = ""


def parse_chat_intent(text: str | None) -> ParsedIntent | None:
    """Parse an owner message in a Business private chat into a structured intent.

    Supported commands:
      - `+100` / `+100.5 备注`   : credit (入账) with optional memo
      - `-50`  / `-50.5 备注`    : debit  (出账) with optional memo
      - `撤销` / `撤` / `-undo`   : void/reverse the latest active in/out entry
      - `/` / `账单` / `查账`     : show formatted balance & recent ledger card
      - `//` / `清账` / `结清`    : settle/clear balance to 0
    """
    if not text:
        return None
    s = text.strip().replace("＋", "+").replace("－", "-").replace("／", "/").replace("．", ".")
    if not s:
        return None

    low = s.lower()
    if s in ("/", "账单", "查账"):
        return ParsedIntent(kind="query")
    if s in ("//", "清账", "结清"):
        return ParsedIntent(kind="settle")
    if s in ("撤销", "撤") or low == "-undo":
        return ParsedIntent(kind="undo")

    # Single-line matching for amounts (allow note on first line, trim & cap length)
    m = _TX_RE.match(s)
    if not m:
        return None
    sign, int_part, frac_part, raw_note = m.groups()
    if len(int_part.lstrip("0") or "0") > MAX_INT_DIGITS:
        return None
    tenths = int(int_part) * 10 + (int(frac_part) if frac_part is not None else 0)
    if tenths <= 0:
        return None
    note = ""
    if raw_note:
        note = " ".join(raw_note.split())[:MAX_NOTE_LEN]
    return ParsedIntent(
        kind="in" if sign == "+" else "out",
        amount_tenths=tenths,
        note=note,
    )


def fmt_num(tenths: int) -> str:
    neg = tenths < 0
    val = abs(tenths)
    whole, frac = divmod(val, 10)
    sign = "-" if neg and val != 0 else ""
    if frac == 0:
        return f"{sign}{whole:,}"
    return f"{sign}{whole:,}.{frac}"


def fmt_plain_num(tenths: int) -> str:
    neg = tenths < 0
    val = abs(tenths)
    whole, frac = divmod(val, 10)
    sign = "-" if neg and val != 0 else ""
    return f"{sign}{whole}" if frac == 0 else f"{sign}{whole}.{frac}"


def fmt_money(tenths: int, unit: str = "USDT") -> str:
    return f"{fmt_num(tenths)} {unit}"


def esc(text: str | None) -> str:
    return html.escape(text or "")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_utc(iso_str: str) -> datetime:
    s = iso_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fmt_local_dt(iso_utc: str, tz_offset_hours: int = 8) -> str:
    try:
        dt = parse_utc(iso_utc).astimezone(timezone(timedelta(hours=tz_offset_hours)))
        return dt.strftime("%m-%d %H:%M")
    except ValueError:
        return iso_utc[:16]


def period_start_utc(period: Literal["today", "week", "month", "all"], tz_offset_hours: int = 8) -> str | None:
    if period == "all":
        return None
    tz = timezone(timedelta(hours=tz_offset_hours))
    now_local = datetime.now(tz)
    if period == "today":
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        monday = now_local - timedelta(days=now_local.weekday())
        start_local = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    else:  # month
        start_local = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def build_receipt_html(
    *,
    kind: Literal["in", "out", "void", "settle"],
    amount_tenths: int,
    balance_after: int,
    journal_id: int | None = None,
    note: str = "",
    unit: str = "USDT",
) -> str:
    note_str = f"  ·  {esc(note)}" if note else ""
    bal_sign = "+" if balance_after > 0 else ""
    if kind == "in":
        return f"<blockquote>{tg_emoji(UP)} <b>入账  <code>+{fmt_money(amount_tenths, unit)}</code></b>{note_str}\n当前余额　<b><code>{bal_sign}{fmt_money(balance_after, unit)}</code></b></blockquote>"
    if kind == "out":
        return f"<blockquote>{tg_emoji(DOWN)} <b>出账  <code>-{fmt_money(amount_tenths, unit)}</code></b>{note_str}\n当前余额　<b><code>{bal_sign}{fmt_money(balance_after, unit)}</code></b></blockquote>"
    if kind == "void":
        return f"<blockquote>{tg_emoji(REFRESH)} <b>已撤销上一笔</b>{note_str}\n当前余额　<b><code>{bal_sign}{fmt_money(balance_after, unit)}</code></b></blockquote>"
    return f"<blockquote>{tg_emoji(OK)} <b>账本已结清</b>\n当前余额　<b><code>0 {esc(unit)}</code></b></blockquote>"
