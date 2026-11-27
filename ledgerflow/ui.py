"""Private-bank inspired Telegram UI design system for LedgerFlow.

Uses Telegram Bot API native features supported by aiogram 3.34:
- Shaded <blockquote> cards and <blockquote expandable> collapsible detail blocks
- NewsEmoji custom emoji icons on cards (<tg-emoji>) and buttons (icon_custom_emoji_id)
- Colored semantic InlineKeyboardButton styles ('primary', 'success', 'danger')
- CopyTextButton for one-tap clipboard copy
"""

from __future__ import annotations

from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from ledgerflow.domain import esc, fmt_local_dt, fmt_money
from ledgerflow.emojis import (
    ANNOUNCE,
    BRONZE,
    CALENDAR,
    CASH,
    CHART,
    DIAMOND,
    DOWN,
    EDIT,
    GOLD,
    GREEN,
    HOME,
    MAIL,
    NO,
    OK,
    PAUSE,
    PIN_LOC,
    PIN_MSG,
    PLAY,
    RED,
    REFRESH,
    SETTINGS,
    SHIELD,
    SILVER,
    TAG,
    TRASH,
    UP,
    tg_emoji,
)
from ledgerflow.repository import Contact, DashboardStats, FinancialReport, JournalEntry, ReceiptItem


def _eid(icon: tuple[str, str] | str | None) -> str | None:
    if icon is None:
        return None
    return icon[0] if isinstance(icon, tuple) else icon


def btn(text: str, cb: str, style: str | None = None, icon: tuple[str, str] | str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb, style=style, icon_custom_emoji_id=_eid(icon))


def btn_primary(text: str, cb: str, icon: tuple[str, str] | str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb, style="primary", icon_custom_emoji_id=_eid(icon))


def btn_success(text: str, cb: str, icon: tuple[str, str] | str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb, style="success", icon_custom_emoji_id=_eid(icon))


def btn_danger(text: str, cb: str, icon: tuple[str, str] | str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb, style="danger", icon_custom_emoji_id=_eid(icon))


def btn_copy(text: str, value: str, style: str | None = None, icon: tuple[str, str] | str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, copy_text=CopyTextButton(text=value), style=style, icon_custom_emoji_id=_eid(icon))


def strip_button_icons(kb: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Return a copy of the keyboard without icon_custom_emoji_id for safe fallback."""
    rows: list[list[InlineKeyboardButton]] = []
    for row in kb.inline_keyboard:
        rows.append([b.model_copy(update={"icon_custom_emoji_id": None}) for b in row])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_tx_line(tx: JournalEntry, unit: str = "USDT", tz_offset: int = 8, show_id: bool = True) -> str:
    ts = fmt_local_dt(tx.created_at, tz_offset)
    display_note = "" if (not show_id and tx.kind == "void") else tx.note
    note = f"  ·  {esc(display_note)}" if display_note else ""
    if tx.kind == "in":
        op = f"<b>+{fmt_money(tx.amount_tenths, unit)}</b>"
    elif tx.kind == "out":
        op = f"<b>-{fmt_money(tx.amount_tenths, unit)}</b>"
    elif tx.kind == "void":
        sign = "+" if tx.delta_tenths >= 0 else ""
        op = f"撤销 <code>{sign}{fmt_money(tx.delta_tenths, unit)}</code>"
    elif tx.kind == "settle":
        op = "结清归零"
    else:
        sign = "+" if tx.delta_tenths >= 0 else ""
        op = f"调账 <code>{sign}{fmt_money(tx.delta_tenths, unit)}</code>"
    prefix = f"<code>#{tx.id}</code>  " if show_id else ""
    line = f"{prefix}<code>{ts}</code>  {op}{note}  →  <code>{fmt_money(tx.balance_after, unit)}</code>"
    return f"<s>{line}</s>" if tx.voided_by_id else line


def render_home_view(d: DashboardStats, unit: str = "USDT") -> tuple[str, InlineKeyboardMarkup]:
    net = d.positive_sum + d.debt_sum
    net_sign = "+" if net > 0 else ""
    text = (
        f"{tg_emoji(DIAMOND)} <b>记账助手</b>  ·  <i>财务总控台</i>\n\n"
        f"<blockquote><b>{tg_emoji(CASH)} 资产总览</b>\n"
        f"客户预存　<code>+{fmt_money(d.positive_sum, unit)}</code>  ({d.positive_count}人)\n"
        f"客户未结　<code>{fmt_money(d.debt_sum, unit)}</code>  ({d.debt_count}人)\n"
        f"账面净额　<b><code>{net_sign}{fmt_money(net, unit)}</code></b></blockquote>\n\n"
        f"<blockquote><b>{tg_emoji(CALENDAR)} 今日动态</b>\n"
        f"入账 <code>+{fmt_money(d.today_in, unit)}</code>  ·  出账 <code>-{fmt_money(d.today_out, unit)}</code>  ·  <code>{d.today_tx_count}</code> 笔\n"
        f"客户账本 <code>{d.total_contacts}</code>（零余额 <code>{d.zero_count}</code>）  ·  待发回执 <code>{d.pending_receipts}</code></blockquote>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                btn_success(f"预存 ({d.positive_count})", "lst:positive:0", icon=GREEN),
                btn_danger(f"未结 ({d.debt_count})", "lst:debt:0", icon=RED),
                btn_primary(f"全部 ({d.total_contacts})", "lst:all:0", icon=CASH),
            ],
            [
                btn("今日活跃", "lst:today:0", icon=CALENDAR),
                btn("标签筛选", "tags:menu", icon=TAG),
            ],
            [
                btn_primary("财务报表", "rep:today", icon=CHART),
                btn("定向通知", "ann:menu", icon=ANNOUNCE),
                btn("系统维护", "cfg:menu", icon=SETTINGS),
                btn("刷新", "home", icon=REFRESH),
            ],
        ]
    )
    return (text, kb)


def render_contact_list_view(
    items: list[Contact],
    total: int,
    mode: str,
    page: int,
    page_size: int,
    unit: str = "USDT",
    sum_balance: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    titles = {
        "all": "全部客户",
        "positive": "预存排行",
        "debt": "未结排行",
        "today": "今日活跃",
    }
    title = f"标签 #{mode[4:]}" if mode.startswith("tag:") else titles.get(mode, "客户列表")
    pages = max(1, (total + page_size - 1) // page_size)
    sum_sign = "+" if sum_balance > 0 else ""
    header = (
        f"{tg_emoji(CASH)} <b>{esc(title)}</b>  ·  第 <code>{page + 1}/{pages}</code> 页\n"
        f"<blockquote>共 <b>{total}</b> 位客户  ·  余额合计 <b><code>{sum_sign}{fmt_money(sum_balance, unit)}</code></b></blockquote>"
    )
    rows: list[list[InlineKeyboardButton]] = []
    for c in items:
        sign = "+" if c.balance_tenths > 0 else ""
        label = f"{c.label}  ·  {sign}{fmt_money(c.balance_tenths, unit)}"
        rows.append([btn(label, f"ct:{c.chat_id}")])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(btn("← 上一页", f"lst:{mode}:{page - 1}"))
    if (page + 1) * page_size < total:
        nav.append(btn("下一页 →", f"lst:{mode}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([btn("返回首页", "home", icon=HOME)])
    return (header, InlineKeyboardMarkup(inline_keyboard=rows))


def render_contact_detail_view(
    c: Contact,
    recent_txs: list[JournalEntry],
    back_cb: str,
    unit: str = "USDT",
    tz_offset: int = 8,
) -> tuple[str, InlineKeyboardMarkup]:
    uname = f"@{esc(c.tg_username)}" if c.tg_username else "无"
    tags_str = " ".join(f"<code>#{esc(t)}</code>" for t in c.tags) if c.tags else "未设置"
    bal_sign = "+" if c.balance_tenths > 0 else ""

    parts = [
        f"{tg_emoji(CASH)} <b>{esc(c.label)}</b>",
        (
            "<blockquote><b>客户档案</b>\n"
            f"TG 名称　{esc(c.tg_display_name)}（{uname}）\n"
            f"数字 ID　<code>{c.chat_id}</code>\n"
            f"标签组　{tags_str}</blockquote>"
        ),
        (
            "<blockquote><b>账本余额</b>\n"
            f"当前余额　<b><code>{bal_sign}{fmt_money(c.balance_tenths, unit)}</code></b>\n"
            f"{tg_emoji(UP)} 累计入账　<code>+{fmt_money(c.total_in_tenths, unit)}</code>\n"
            f"{tg_emoji(DOWN)} 累计出账　<code>-{fmt_money(c.total_out_tenths, unit)}</code>\n"
            f"流水笔数　<code>{c.tx_count}</code> 笔</blockquote>"
        ),
    ]
    if recent_txs:
        tx_lines = "\n".join(format_tx_line(tx, unit, tz_offset) for tx in recent_txs)
        parts.append(f"<blockquote expandable><b>最近 {len(recent_txs)} 笔流水（点击展开）</b>\n{tx_lines}</blockquote>")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                btn_primary("后台调账", f"adj:{c.chat_id}", icon=EDIT),
                btn_danger("结清归零", f"stl_ask:{c.chat_id}", icon=TRASH),
            ],
            [
                btn("全部流水", f"hist:{c.chat_id}:0", icon=CHART),
                btn("定位聊天", f"loc:{c.chat_id}", icon=PIN_LOC),
            ],
            [
                btn("设别名", f"alias:{c.chat_id}", icon=EDIT),
                btn("设标签", f"tagset:{c.chat_id}", icon=TAG),
                btn_copy("复制 ID", str(c.chat_id)),
            ],
            [
                btn("← 返回列表", back_cb),
                btn("首页", "home", icon=HOME),
            ],
        ]
    )
    return ("\n\n".join(parts), kb)


def render_history_view(
    c: Contact | None,
    chat_id: int,
    txs: list[JournalEntry],
    total: int,
    page: int,
    page_size: int,
    unit: str = "USDT",
    tz_offset: int = 8,
) -> tuple[str, InlineKeyboardMarkup]:
    pages = max(1, (total + page_size - 1) // page_size)
    name = esc(c.label) if c else str(chat_id)
    body = "\n".join(format_tx_line(tx, unit, tz_offset) for tx in txs) if txs else "<i>暂无流水记录</i>"
    text = (
        f"{tg_emoji(CHART)} <b>{name}</b>  ·  流水明细（第 <code>{page + 1}/{pages}</code> 页，共 <code>{total}</code> 笔）\n\n"
        f"<blockquote>{body}</blockquote>"
    )
    rows: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(btn("← 上一页", f"hist:{chat_id}:{page - 1}"))
    if (page + 1) * page_size < total:
        nav.append(btn("下一页 →", f"hist:{chat_id}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([btn("← 返回客户", f"ct:{chat_id}"), btn("首页", "home", icon=HOME)])
    return (text, InlineKeyboardMarkup(inline_keyboard=rows))


def render_report_view(rep: FinancialReport, unit: str = "USDT") -> tuple[str, InlineKeyboardMarkup]:
    labels = {"today": "今日", "week": "本周", "month": "本月", "all": "全部历史"}
    period = rep.period
    net_sign = "+" if rep.net_flow >= 0 else ""
    medals = (GOLD, SILVER, BRONZE)

    blocks = [
        f"{tg_emoji(CHART)} <b>财务报表</b>  ·  <i>{labels[period]}</i>",
        (
            "<blockquote><b>收支汇总</b>\n"
            f"{tg_emoji(UP)} 总入账　<code>+{fmt_money(rep.total_in, unit)}</code>\n"
            f"{tg_emoji(DOWN)} 总出账　<code>-{fmt_money(rep.total_out, unit)}</code>\n"
            f"{tg_emoji(DIAMOND)} 净流入　<b><code>{net_sign}{fmt_money(rep.net_flow, unit)}</code></b>\n"
            f"有效笔数 <code>{rep.tx_count}</code> 笔  ·  活跃客户 <code>{rep.active_contacts}</code> 人</blockquote>"
        ),
    ]
    if rep.top_positive:
        lines = "\n".join(
            f"{tg_emoji(medals[i - 1]) if i <= 3 else f'{i}.'} {esc(c.label)}　<code>+{fmt_money(c.balance_tenths, unit)}</code>"
            for i, c in enumerate(rep.top_positive, 1)
        )
        blocks.append(f"<blockquote expandable><b>预存 Top 5（点击展开）</b>\n{lines}</blockquote>")
    if rep.top_debt:
        lines = "\n".join(
            f"{tg_emoji(medals[i - 1]) if i <= 3 else f'{i}.'} {esc(c.label)}　<code>{fmt_money(c.balance_tenths, unit)}</code>"
            for i, c in enumerate(rep.top_debt, 1)
        )
        blocks.append(f"<blockquote expandable><b>未结 Top 5（点击展开）</b>\n{lines}</blockquote>")

    def pbtn(key: str, text: str) -> InlineKeyboardButton:
        return btn_primary(text, f"rep:{key}") if period == key else btn(text, f"rep:{key}")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [pbtn("today", "今日"), pbtn("week", "本周"), pbtn("month", "本月"), pbtn("all", "全部")],
            [btn("返回首页", "home", icon=HOME)],
        ]
    )
    return ("\n\n".join(blocks), kb)


def render_settings_view(d: DashboardStats) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"{tg_emoji(SETTINGS)} <b>系统维护与设置</b>\n\n"
        "<blockquote><b>运行状态</b>\n"
        f"待发回执数　<code>{d.pending_receipts}</code> 条\n"
        f"回执自动置顶　<code>{'已开启' if d.auto_pin else '已关闭'}</code></blockquote>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [btn_primary(f"自动置顶：{'开' if d.auto_pin else '关'}（点击切换）", "cfg:pin", icon=PIN_MSG)],
            [
                btn(f"待发回执 ({d.pending_receipts})", "rcpt:0", icon=MAIL),
                btn("立即重试回执", "rcpt_retry", icon=REFRESH),
            ],
            [
                btn_success("立即热备份账本", "cfg:backup", icon=SHIELD),
                btn("账本健康体检", "cfg:health", icon=OK),
            ],
            [
                btn("返回首页", "home", icon=HOME),
            ],
        ]
    )
    return (text, kb)


def render_receipts_view(
    items: list[ReceiptItem],
    total: int,
    page: int = 0,
    page_size: int = 10,
) -> tuple[str, InlineKeyboardMarkup]:
    pages = max(1, (total + page_size - 1) // page_size)
    if not items:
        body = "当前队列为空，所有回执均已送达。"
    else:
        body = "\n".join(
            f"<code>#{r.id}</code> 客户 <code>{r.chat_id}</code> · 尝试 <code>{r.attempts}</code> 次"
            + (f" · {esc(r.last_error[:36])}" if r.last_error else "")
            for r in items
        )
    text = f"{tg_emoji(MAIL)} <b>待发回执队列</b>（共 <code>{total}</code> 条 · 第 <code>{page + 1}/{pages}</code> 页）\n\n<blockquote>{body}</blockquote>"
    rows: list[list[InlineKeyboardButton]] = []
    if total > 0:
        rows.append([btn_primary("立即重试全部", "rcpt_retry", icon=REFRESH)])
        for i in range(0, len(items), 2):
            rows.append([btn_danger(f"忽略 #{r.id}", f"rcpt_del:{r.id}:{page}", icon=TRASH) for r in items[i : i + 2]])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(btn("← 上一页", f"rcpt:{page - 1}"))
    if (page + 1) * page_size < total:
        nav.append(btn("下一页 →", f"rcpt:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([btn("← 返回维护", "cfg:menu"), btn("首页", "home", icon=HOME)])
    return (text, InlineKeyboardMarkup(inline_keyboard=rows))


def render_announce_status_view(
    ann_id: int,
    status: str,
    target_desc: str,
    counts: dict[str, int],
    allow_new: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"{tg_emoji(ANNOUNCE)} <b>定向通知 #{ann_id}</b>  ·  <code>{status}</code>\n\n"
        "<blockquote><b>投递进度</b>\n"
        f"目标范围　<code>{esc(target_desc)}</code>\n"
        f"等待 <code>{counts['pending']}</code>  ·  发送中 <code>{counts['sending']}</code>  ·  已发 <code>{counts['sent']}</code>\n"
        f"失败 <code>{counts['failed']}</code>  ·  不确定 <code>{counts['uncertain']}</code>  ·  已取消 <code>{counts['cancelled']}</code></blockquote>"
    )
    rows: list[list[InlineKeyboardButton]] = []
    if status == "running":
        rows.append([btn("暂停发送", f"ann:pause:{ann_id}", icon=PAUSE), btn_danger("取消任务", f"ann:cancel:{ann_id}", icon=NO)])
    elif status == "paused":
        rows.append([btn_primary("继续发送", f"ann:resume:{ann_id}", icon=PLAY), btn_danger("取消任务", f"ann:cancel:{ann_id}", icon=NO)])
    if allow_new:
        rows.append([btn_primary("新建定向通知", "ann:new", icon=ANNOUNCE)])
    rows.append([btn("刷新进度", "ann:menu", icon=REFRESH), btn("返回首页", "home", icon=HOME)])
    return (text, InlineKeyboardMarkup(inline_keyboard=rows))


def render_announce_picker_view(tags: list[str]) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"{tg_emoji(ANNOUNCE)} <b>新建定向通知</b>\n\n"
        "<blockquote>请选择本次通知的目标客户范围（静默发送，1.2 秒/条，不影响记账回执）。</blockquote>"
    )
    rows = [
        [btn_primary("全部有账本客户", "ann:pick:all", icon=CASH)],
        [btn_success("预存客户（余额 > 0）", "ann:pick:positive", icon=GREEN), btn("非负余额（含零）", "ann:pick:non_negative")],
        [btn_danger("未结客户（余额 < 0）", "ann:pick:debt", icon=RED)],
    ]
    for i in range(0, min(len(tags), 6), 2):
        row = [btn(f"标签 #{t}", f"ann:pick:tag:{t}", icon=TAG) for t in tags[i : i + 2]]
        rows.append(row)
    rows.append([btn("返回首页", "home", icon=HOME)])
    return (text, InlineKeyboardMarkup(inline_keyboard=rows))
