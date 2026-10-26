from __future__ import annotations

import inspect
import logging
import re
from html import escape

from telegram import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.error import (
    BadRequest,
    TelegramError,
)

from jzbot.domain import (
    ADMIN_PAGE_SIZE,
    ADMIN_SEARCH_MAX_LENGTH,
    OUTBOX_ATTENTION_ATTEMPTS,
    OUTBOX_ATTENTION_MINUTES,
    LocateFailureCategory,
    admin_page_bounds,
    beijing_display_from_utc,
    classify_locate_error,
    customer_display_name,
    format_signed_tenths,
    format_tenths,
    trim_button_text,
)
from jzbot.infra.rich import edit_admin_rich, rich_button_rows

logger = logging.getLogger("jzbot")


def admin_html(value: str, max_length: int = 80) -> str:
    return escape(trim_button_text(value, max_length), quote=True)


def admin_rich_table(rows, headings=("项目", "数值")) -> str:
    """Native Rich HTML cells; inputs are plain text and escaped exactly once."""
    header = (
        "<tr>" + "".join(f"<th>{escape(label)}</th>" for label in headings) + "</tr>"
    )
    body = "".join(
        f"<tr><td>{escape(str(label))}</td><td><b>{escape(str(value))}</b></td></tr>"
        for label, value in rows
    )
    return f"<table bordered striped compact>{header}{body}</table>"


def admin_rich_page(
    content: str, keyboard, footer: str, *, controls: str | None = None
):
    """Keep one control definition for Rich body buttons and API compatibility."""
    controls = rich_button_rows(keyboard) if controls is None else controls
    return content + "<hr/>" + controls + f"<footer>{escape(footer)}</footer>", keyboard


def customer_summary_text(detail) -> str | None:
    """Copy at most 256 UTF-16 units; never truncate any financial figure."""
    wallet = detail["wallet"]
    fixed = (
        f"\n当前余额：{format_tenths(int(wallet['balance_tenths']))}U"
        f"\n累计入金：{format_tenths(detail['deposit_total'])}U"
        f"\n累计出金：{format_tenths(detail['withdraw_total'])}U"
        f"\n记账次数：{detail['entry_count']}"
    )
    budget = 256 - len(("客户：" + fixed).encode("utf-16-le")) // 2
    if budget < 2:
        return None  # Giant integers cannot fit; do not corrupt the amounts.
    name = " ".join(customer_display_name(wallet).split())
    if len(name.encode("utf-16-le")) // 2 > budget:
        clipped = []
        remaining = budget - 1
        for char in name:
            width = len(char.encode("utf-16-le")) // 2
            if width > remaining:
                break
            clipped.append(char)
            remaining -= width
        name = "".join(clipped) + "…"
    return "客户：" + name + fixed


def admin_button(text: str, callback_data: str, style: str | None = None):
    if not 1 <= len(callback_data.encode("utf-8")) <= 64:
        raise ValueError("Admin callback_data exceeds Telegram limits")
    kwargs = {}
    if style and "style" in inspect.signature(InlineKeyboardButton).parameters:
        kwargs["style"] = style
    return InlineKeyboardButton(text, callback_data=callback_data, **kwargs)


def admin_navigation(back_callback: str = "home", back_text: str = "🔙 返回"):
    return [
        admin_button(back_text, back_callback),
        admin_button("🏠 首页", "home"),
    ]


def admin_pagination(page: int, pages: int, callback_for_page):
    buttons = []
    if page > 0:
        buttons.append(admin_button("⬅️ 上一页", callback_for_page(page - 1)))
    if page + 1 < pages:
        buttons.append(admin_button("下一页 ➡️", callback_for_page(page + 1)))
    return [buttons] if buttons else []


def customer_button_label(row) -> str:
    name = customer_display_name(row)
    if len(name) > 22:
        # Preserve both balance and identity for similarly named long customers.
        name = f"{trim_button_text(name, 22)} · {int(row['customer_chat_id'])}"
    state = "已停用 · " if "is_disabled" in row.keys() and row["is_disabled"] else ""
    return f"{state}{name}｜{format_signed_tenths(int(row['balance_tenths']))}U"


def admin_entry_line(row) -> str:
    timestamp = beijing_display_from_utc(row["created_at_utc"])
    if row["entry_type"] == "clear":
        receipt = (
            "管理员归零"
            if "operation_origin" in row.keys() and row["operation_origin"] == "admin"
            else "账本已清空"
        )
    else:
        sign = "+" if row["entry_type"] == "deposit" else "-"
        receipt = (
            f"{sign}{format_tenths(int(row['amount_tenths']))}｜"
            f"余{format_tenths(int(row['balance_after']))}U"
        )
    return f"{timestamp}　{escape(receipt)}"


def build_home_view(wallet_rows, outbox_attention_count: int = 0):
    total_count = len(wallet_rows)
    positive = []
    negative = []
    zero_count = 0

    for row in wallet_rows:
        balance = int(row["balance_tenths"])
        if balance > 0:
            positive.append(balance)
        elif balance < 0:
            negative.append(balance)
        else:
            zero_count += 1

    deposit_total = sum(positive)
    debt_total = sum(-value for value in negative)
    net_total = sum(positive) + sum(negative)

    text = (
        "<h2>账本管理</h2>"
        f"<p>当前净余额<br><b>{format_tenths(net_total)} U</b></p>"
        + admin_rich_table(
            [
                ("客户总数", f"{total_count} 人"),
                ("存款用户", f"{len(positive)} 人"),
                ("欠款用户", f"{len(negative)} 人"),
                ("零余额用户", f"{zero_count} 人"),
                ("存款总额", f"{format_tenths(deposit_total)} U"),
                ("欠款总额", f"{format_tenths(debt_total)} U"),
            ],
            headings=("账本概览", "统计数值"),
        )
    )
    rows = [
        [
            admin_button("🟢 存款用户", "list:p:0", "success"),
            admin_button("🔴 欠款用户", "list:d:0", "danger"),
        ],
        [
            admin_button("👥 全部用户", "list:a:0"),
            admin_button("🔍 搜索客户", "search", "primary"),
        ],
        [admin_button("🔄 刷新", "home")],
        [
            admin_button("有入金记录", "list:i:0"),
            admin_button("待交付回执", "receipts:0"),
        ],
    ]
    if outbox_attention_count:
        rows.append(
            [admin_button(f"⚠️ 回执异常（{outbox_attention_count}）", "outbox:0")]
        )
    keyboard = InlineKeyboardMarkup(rows)
    disabled = sum(
        1 for row in wallet_rows if "is_disabled" in row.keys() and row["is_disabled"]
    )
    if disabled:
        text += f"<p>已标记停用：<b>{disabled} 人</b> · 金额仍计入统计</p>"
    return admin_rich_page(
        text + "<h3>客户管理</h3>", keyboard, "仅用于管理员私聊 · 金额单位 U"
    )


def filter_and_sort_wallet_rows(wallet_rows, kind: str):
    enriched = []
    for row in wallet_rows:
        balance = int(row["balance_tenths"])
        if kind == "p" and balance <= 0:
            continue
        if kind == "d" and balance >= 0:
            continue

        enriched.append((row, balance))

    if kind == "p":
        enriched.sort(key=lambda item: item[1], reverse=True)
    elif kind == "d":
        # -1000 排在 -20 前面
        enriched.sort(key=lambda item: item[1])
    else:
        # 全部用户按最近记账时间倒序
        enriched.sort(key=lambda item: item[0]["updated_at"], reverse=True)

    return enriched


def build_customer_list_view(wallet_rows, kind: str, page: int):
    labels = {
        "i": ("有入金记录的客户", "当前净余额"),
        "p": ("存款用户", "存款总额"),
        "d": ("欠款用户", "欠款总额"),
        "a": ("全部用户", "净余额"),
    }
    title, total_label = labels.get(kind, labels["a"])
    kind = kind if kind in labels else "a"

    items = filter_and_sort_wallet_rows(wallet_rows, kind)
    total_items = len(items)

    if kind == "p":
        total_balance = sum(balance for _, balance in items)
    elif kind == "d":
        total_balance = sum(-balance for _, balance in items)
    else:
        total_balance = sum(balance for _, balance in items)

    page, page_count = admin_page_bounds(total_items, page)
    start = page * ADMIN_PAGE_SIZE
    current = items[start : start + ADMIN_PAGE_SIZE]

    text = (
        f"<h2>{title}</h2>"
        f"<p>共 {total_items} 人｜第 {page + 1} / {page_count} 页<br>"
        f"{total_label}：<b>{format_tenths(total_balance)} U</b></p>"
    )

    rows = []
    for row, balance in current:
        button_text = customer_button_label(row)
        rows.append(
            [
                InlineKeyboardButton(
                    button_text,
                    callback_data=(
                        f"user:{int(row['customer_chat_id'])}:{kind}:{page}"
                    ),
                )
            ]
        )

    if not current:
        text += "<p>暂无用户</p>"

    rows.extend(
        admin_pagination(page, page_count, lambda target: f"list:{kind}:{target}")
    )
    rows.append(admin_navigation())
    return admin_rich_page(
        text, InlineKeyboardMarkup(rows), "每页最多 10 人 · 点击客户查看账本"
    )


def build_search_results_view(
    search_query: str,
    wallet_rows,
    total_items: int,
    page: int,
):
    page, page_count = admin_page_bounds(total_items, page)
    text = (
        "<h2>搜索结果</h2>"
        f"<p>关键词：{admin_html(search_query, ADMIN_SEARCH_MAX_LENGTH)}<br>"
        f"找到 {total_items} 位客户｜第 {page + 1} / {page_count} 页</p>"
    )
    rows = []

    for row in wallet_rows:
        rows.append(
            [
                InlineKeyboardButton(
                    customer_button_label(row),
                    callback_data=(f"user:{int(row['customer_chat_id'])}:s:{page}"),
                )
            ]
        )

    if not wallet_rows:
        text += "<p>没有匹配的客户</p>"

    rows.extend(
        admin_pagination(page, page_count, lambda target: f"search_page:{target}")
    )
    rows.append([admin_button("🔍 重新搜索", "search", "primary")])
    rows.append(admin_navigation())
    return admin_rich_page(
        text, InlineKeyboardMarkup(rows), "每页最多 10 人 · 仅搜索当前账本"
    )


def build_customer_detail_view(
    detail,
    back_kind: str,
    back_page: int,
    include_chat_button: bool = True,
    notice: str | None = None,
):
    wallet = detail["wallet"]
    name = customer_display_name(wallet)
    username = (wallet["username"] or "").strip()
    balance = int(wallet["balance_tenths"])
    recent = detail["recent"]
    disabled = bool(wallet["is_disabled"]) if "is_disabled" in wallet.keys() else False

    lines = [f"<h2>客户账本 · {admin_html(name)}</h2>"]
    if notice:
        lines.append(f"<p>{escape(notice)}</p>")
    if username and name != f"@{username}":
        lines.append(f"<p>{admin_html('@' + username.lstrip('@'))}</p>")

    lines.extend(
        [
            f"<p>当前余额<br><b>{format_tenths(balance)} U</b></p>",
            admin_rich_table(
                [
                    ("客户状态", "已停用" if disabled else "正常"),
                    ("累计入金", f"{format_tenths(detail['deposit_total'])} U"),
                    ("累计出金", f"{format_tenths(detail['withdraw_total'])} U"),
                    ("记账次数", f"{detail['entry_count']} 笔"),
                    (
                        "最后记账",
                        beijing_display_from_utc(recent[0]["created_at_utc"])
                        if recent
                        else "暂无",
                    ),
                ],
                headings=("客户统计", "数值"),
            ),
        ]
    )

    summary = customer_summary_text(detail)
    copy_row = []
    if summary is not None:
        copy_row = [InlineKeyboardButton("复制摘要", copy_text=CopyTextButton(summary))]
    else:
        lines.append("<p>账务摘要超过复制长度限制，请查看上方完整金额。</p>")
    lines.append("<details><summary>最近 5 笔流水</summary>")
    lines.extend(f"<p>{admin_entry_line(row)}</p>" for row in recent[:5])
    if not recent:
        lines.append("<p>暂无流水记录</p>")
    lines.append("</details>")

    # 不再使用 tg://user?id=...。这种用户 ID 深链会受对方隐私设置影响，
    # 某些客户会导致整张详情卡片编辑失败。
    customer_chat_id = int(wallet["customer_chat_id"])
    keyboard_rows = [
        copy_row
        + [
            admin_button(
                "📒 全部流水",
                f"history:{customer_chat_id}:0:{back_kind}:{back_page}",
                "primary",
            )
        ]
    ]
    chat_buttons = []

    if include_chat_button and username:
        clean_username = username.lstrip("@")
        # 有公开 username 时，可以直接打开聊天。
        if re.fullmatch(r"[A-Za-z0-9_]{5,32}", clean_username):
            chat_buttons.append(
                InlineKeyboardButton(
                    "💬 打开聊天", url=f"https://t.me/{clean_username}"
                )
            )

    # “定位聊天”不依赖 username。点击后会通过 Business 连接给客户
    # 静默发送一个 "."，让该会话重新出现在最近聊天里。
    chat_buttons.append(
        admin_button(
            "📍 定位聊天", f"locate:{customer_chat_id}:{back_kind}:{back_page}"
        )
    )
    keyboard_rows.append(chat_buttons)
    keyboard_rows.append(
        [
            admin_button(
                "恢复正常" if disabled else "标记停用",
                f"state:{customer_chat_id}:{int(not disabled)}:{back_kind}:{back_page}",
            ),
            admin_button(
                "管理员归零",
                f"zero:{customer_chat_id}:{back_kind}:{back_page}",
                "danger",
            ),
        ]
    )
    # Reuse the existing owner-scoped detail callback: it reloads the wallet,
    # statistics and recent entries, and rebuilds copy_text in the same edit.
    keyboard_rows.append(
        [admin_button("🔄 刷新", f"user:{customer_chat_id}:{back_kind}:{back_page}")]
    )

    back_callback = (
        f"search_page:{back_page}"
        if back_kind == "s"
        else f"list:{back_kind}:{back_page}"
    )
    keyboard_rows.append(admin_navigation(back_callback))

    keyboard = InlineKeyboardMarkup(keyboard_rows)
    return admin_rich_page(
        "".join(lines),
        keyboard,
        "摘要为页面快照 · 复制前请刷新 · 停用仅为内部标记",
    )


def build_admin_zero_confirmation(detail, request):
    wallet = detail["wallet"]
    content = (
        f"<h2>管理员归零 · {admin_html(customer_display_name(wallet))}</h2>"
        + admin_rich_table(
            [
                ("Telegram ID", str(wallet["customer_chat_id"])),
                ("当前余额", f"{format_tenths(int(request['expected_balance']))} U"),
            ],
            headings=("确认客户", "当前数据"),
        )
        + "<p>确认后余额归零，保留历史和累计收支。<br>"
        "不向客户发送消息，也不置顶。请核对客户及金额。</p>"
    )
    nonce = request["operation_id"]
    keyboard = InlineKeyboardMarkup(
        [
            [
                admin_button("确认归零", f"zero_confirm:{nonce}", "danger"),
                admin_button("取消操作", f"zero_cancel:{nonce}"),
            ]
        ]
    )
    return admin_rich_page(
        content, keyboard, "确认有效期 5 分钟 · 余额变化后须重新确认"
    )


def build_customer_history_view(
    detail, entries, total: int, page: int, back_kind: str, back_page: int
):
    wallet = detail["wallet"]
    customer = int(wallet["customer_chat_id"])
    page, pages = admin_page_bounds(total, page)
    lines = [
        f"<h2>历史流水 · {admin_html(customer_display_name(wallet))}</h2>",
        f"<p>当前余额：<b>{format_tenths(int(wallet['balance_tenths']))} U</b><br>"
        f"共 {total} 笔｜第 {page + 1} / {pages} 页</p>",
    ]
    if entries:
        for row in entries:
            timestamp, receipt = admin_entry_line(row).split("　", 1)
            lines.append(f"<p>{timestamp}<br><b>{receipt}</b></p>")
    if not entries:
        lines.append("<p>暂无流水记录</p>")
    rows = admin_pagination(
        page,
        pages,
        lambda target: f"history:{customer}:{target}:{back_kind}:{back_page}",
    )
    rows.append(
        admin_navigation(f"user:{customer}:{back_kind}:{back_page}", "🔙 返回客户")
    )
    return admin_rich_page(
        "".join(lines), InlineKeyboardMarkup(rows), "每页 10 笔 · 最新记录在前"
    )


def outbox_error_summary(error: str | None) -> str:
    # Never display the raw error: it can contain API URLs or credentials.
    if not error:
        return "暂无错误信息"
    category = classify_locate_error(TelegramError(error))
    return {
        LocateFailureCategory.CONNECTION_INVALID: "Business 连接失效",
        LocateFailureCategory.CONNECTION_DISABLED: "Business 连接已停用",
        LocateFailureCategory.NO_REPLY_PERMISSION: "Business 代发权限不足",
        LocateFailureCategory.CUSTOMER_UNREACHABLE: "客户当前无法主动联系",
        LocateFailureCategory.CUSTOMER_BLOCKED: "客户限制发送消息",
        LocateFailureCategory.TEMPORARY: "Telegram 临时响应失败",
    }.get(category, "发送失败，具体原因需查看服务器日志")


def build_outbox_attention_view(entries, total: int, page: int):
    page, pages = admin_page_bounds(total, page)
    lines = [
        "<h2>回执异常</h2>",
        f"<p>共 {total} 条｜第 {page + 1} / {pages} 页</p>",
        "<details><summary>提醒依据</summary>"
        f"<p>判定：待投递且失败 ≥{OUTBOX_ATTENTION_ATTEMPTS} 次，"
        f"或等待 ≥{OUTBOX_ATTENTION_MINUTES} 分钟。</p></details>",
    ]
    for row in entries:
        attempts = int(row["attempt_count"])
        status = (
            f"多次发送失败（{attempts} 次），待投递"
            if attempts >= OUTBOX_ATTENTION_ATTEMPTS
            else f"长时间未投递（失败 {attempts} 次）"
        )
        lines.extend(
            [
                f"<p>客户：<b>{admin_html(customer_display_name(row), 40)}</b><br>"
                f"状态：{status}<br>"
                f"创建：{beijing_display_from_utc(row['created_at_utc'])}<br>"
                f"最近错误：{outbox_error_summary(row['last_error'])}</p>",
            ]
        )
    if not entries:
        lines.append("<p>暂无需要关注的回执异常</p>")
    rows = admin_pagination(page, pages, lambda target: f"outbox:{target}")
    rows.append([admin_button("🔄 刷新", f"outbox:{page}")])
    rows.append(admin_navigation())
    return admin_rich_page(
        "".join(lines), InlineKeyboardMarkup(rows), "仅供查看，自动重试仍在继续。"
    )


async def safe_edit_query(query, text: str, reply_markup: InlineKeyboardMarkup):
    if "<h2>" not in text:
        text, reply_markup = build_admin_notice_view("管理提示", text, reply_markup)
    try:
        await edit_admin_rich(query, text, reply_markup)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


def build_admin_notice_view(title: str, message: str, keyboard):
    return admin_rich_page(
        f"<h2>{escape(title)}</h2><p>{escape(message)}</p>",
        keyboard,
        "仅用于管理员私聊",
    )
