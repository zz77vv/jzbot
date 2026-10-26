from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo

from telegram.error import (
    BadRequest,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)

logger = logging.getLogger("jzbot")


BEIJING_TZ = ZoneInfo("Asia/Shanghai")

ADMIN_PAGE_SIZE = 10

ADMIN_SEARCH_MAX_LENGTH = 64

OUTBOX_ATTENTION_ATTEMPTS = 3

OUTBOX_ATTENTION_MINUTES = 15

OUTBOX_BATCH_SIZE = 20

OUTBOX_POLL_SECONDS = 10

AMOUNT_RE = re.compile(r"^([+-])(\d+)(?:\.(\d))?$")

WELCOME_TEXT = """记账机器人已连接成功

+10  入金 10 USDT
-10  出金 10 USDT
+0.5 / -0.5  支持 0.1 USDT

/   查看最近 5 条记录
//  清账

仅您本人发送的记账指令有效。
每位客户使用独立账本。

私聊机器人发送 /start 可打开账本管理。"""


class LocateFailureCategory(str, Enum):
    NO_CONNECTION = "no_connection"
    CONNECTION_INVALID = "connection_invalid"
    CONNECTION_DISABLED = "connection_disabled"
    NO_REPLY_PERMISSION = "no_reply_permission"
    CUSTOMER_UNREACHABLE = "customer_unreachable"
    CUSTOMER_BLOCKED = "customer_blocked"
    TEMPORARY = "temporary"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LocateResult:
    success: bool
    connection_id: str | None = None
    failure_category: LocateFailureCategory | None = None


LOCATE_FAILURE_MESSAGES = {
    LocateFailureCategory.NO_CONNECTION: (
        "没有可用的 Business 连接，请重新连接机器人后再试"
    ),
    LocateFailureCategory.CONNECTION_INVALID: (
        "Business 连接已失效，请重新连接机器人后再试"
    ),
    LocateFailureCategory.CONNECTION_DISABLED: (
        "Business 连接当前已停用，请重新连接机器人后再试"
    ),
    LocateFailureCategory.NO_REPLY_PERMISSION: (
        "当前 Business 机器人没有代发消息权限，请检查 Telegram Business 连接权限"
    ),
    LocateFailureCategory.CUSTOMER_UNREACHABLE: (
        "该客户当前无法通过 Business Bot 主动联系，"
        "可能已超出可回复条件或聊天状态不允许发送"
    ),
    LocateFailureCategory.CUSTOMER_BLOCKED: ("该客户当前不允许机器人发送消息"),
    LocateFailureCategory.TEMPORARY: ("Telegram 暂时响应失败，请稍后再试"),
    LocateFailureCategory.UNKNOWN: "定位失败，请稍后重试",
}


def parse_amount_to_tenths(text: str):
    text = text.strip()
    match = AMOUNT_RE.fullmatch(text)
    if not match:
        return None

    sign, whole, decimal_digit = match.groups()
    decimal_digit = decimal_digit or "0"

    try:
        amount_tenths = int(whole) * 10 + int(decimal_digit)
    except (ValueError, OverflowError):
        return None

    # +0 / -0 / +0.0 / -0.0 全部忽略
    if amount_tenths == 0:
        return None

    if sign == "-":
        amount_tenths = -amount_tenths

    return amount_tenths


def format_tenths(value: int) -> str:
    sign = "-" if value < 0 else ""
    value = abs(value)
    whole, tenth = divmod(value, 10)

    if tenth == 0:
        return f"{sign}{whole}"

    return f"{sign}{whole}.{tenth}"


def format_signed_tenths(value: int) -> str:
    if value > 0:
        return f"+{format_tenths(value)}"
    return format_tenths(value)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_after_iso(seconds: int, now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    return (current + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def outbox_backoff_seconds(attempt_count: int) -> int:
    """Bounded retry schedule: 10s, 30s, 2m, then at most 5m."""
    schedule = (10, 30, 120, 300)
    index = max(0, min(attempt_count - 1, len(schedule) - 1))
    return schedule[index]


def beijing_display_from_utc(utc_iso: str) -> str:
    dt = datetime.fromisoformat(utc_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BEIJING_TZ).strftime("%m-%d %H:%M")


def customer_display_name(row) -> str:
    first_name = (row["first_name"] or "").strip() if "first_name" in row.keys() else ""
    last_name = (row["last_name"] or "").strip() if "last_name" in row.keys() else ""
    username = (row["username"] or "").strip() if "username" in row.keys() else ""

    full_name = " ".join(part for part in (first_name, last_name) if part)
    if full_name:
        return full_name
    if username:
        return f"@{username}"
    return f"用户 {int(row['customer_chat_id'])}"


def trim_button_text(text: str, max_len: int = 48) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def classify_locate_error(exc: TelegramError) -> LocateFailureCategory:
    """Conservatively classify stable Telegram error types and keywords."""
    error_text = str(exc).lower()
    normalized = re.sub(r"[_-]+", " ", error_text)

    if any(
        phrase in normalized
        for phrase in (
            "business connection invalid",
            "business connection not found",
            "connection not found",
        )
    ):
        return LocateFailureCategory.CONNECTION_INVALID

    if any(
        phrase in normalized
        for phrase in (
            "business connection disabled",
            "connection disabled",
        )
    ):
        return LocateFailureCategory.CONNECTION_DISABLED

    if any(
        phrase in normalized
        for phrase in (
            "not enough rights",
            "permission denied",
            "no permission",
            "not allowed to send messages on behalf",
            "cannot reply",
            "can't reply",
            "can reply false",
        )
    ):
        return LocateFailureCategory.NO_REPLY_PERMISSION

    if isinstance(exc, (TimedOut, RetryAfter)) or (
        isinstance(exc, NetworkError) and not isinstance(exc, BadRequest)
    ):
        return LocateFailureCategory.TEMPORARY

    if any(
        phrase in normalized
        for phrase in (
            "timed out",
            "timeout",
            "network error",
            "temporary server error",
            "bad gateway",
            "retry after",
            "too many requests",
        )
    ):
        return LocateFailureCategory.TEMPORARY

    if any(
        phrase in normalized
        for phrase in (
            "bot was blocked",
            "blocked by the user",
            "user is deactivated",
            "chat unavailable",
        )
    ):
        return LocateFailureCategory.CUSTOMER_BLOCKED

    if any(
        phrase in normalized
        for phrase in (
            "business peer usage missing",
            "peer id invalid",
            "cannot initiate conversation",
            "can't initiate conversation",
            "chat not found",
            "not allowed to write to the user",
        )
    ):
        return LocateFailureCategory.CUSTOMER_UNREACHABLE

    return LocateFailureCategory.UNKNOWN


def locate_failure_message(category: LocateFailureCategory | None) -> str:
    return LOCATE_FAILURE_MESSAGES.get(
        category,
        LOCATE_FAILURE_MESSAGES[LocateFailureCategory.UNKNOWN],
    )


def log_locate_failure(
    owner_user_id: int,
    customer_chat_id: int,
    connection_id: str,
    category: LocateFailureCategory,
    exc: TelegramError,
) -> None:
    retry_after = getattr(exc, "retry_after", None)
    log_method = (
        logger.error if category == LocateFailureCategory.UNKNOWN else logger.warning
    )
    log_method(
        "Locate chat failed | owner=%s customer=%s business_connection=%s "
        "failure_category=%s exception_type=%s error=%s retry_after=%s",
        owner_user_id,
        customer_chat_id,
        connection_id,
        category.value,
        type(exc).__name__,
        exc,
        retry_after,
    )


def admin_page_bounds(total: int, page: int) -> tuple[int, int]:
    pages = max(1, (total + ADMIN_PAGE_SIZE - 1) // ADMIN_PAGE_SIZE)
    return max(0, min(page, pages - 1)), pages


def normalize_admin_search(query: str) -> str:
    query = query.strip()
    if len(query) > ADMIN_SEARCH_MAX_LENGTH:
        return ""
    return query.removeprefix("@").strip()


def outbox_attention_cutoff(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(minutes=OUTBOX_ATTENTION_MINUTES)).isoformat()
