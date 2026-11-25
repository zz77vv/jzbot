"""Private-bank inspired Telegram UI design system for LedgerFlow.

Uses Telegram Bot API native features supported by aiogram 3.34:
- Shaded <blockquote> cards and <blockquote expandable> collapsible detail blocks
- Colored semantic InlineKeyboardButton styles ('primary', 'success', 'danger')
- CopyTextButton for one-tap clipboard copy
"""

from __future__ import annotations

from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from ledgerflow.domain import esc, fmt_local_dt, fmt_money
from ledgerflow.repository import Contact, DashboardStats, FinancialReport, JournalEntry, ReceiptItem


def btn(text: str, cb: str, style: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb, style=style)


def btn_primary(text: str, cb: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb, style="primary")


def btn_success(text: str, cb: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb, style="success")


def btn_danger(text: str, cb: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb, style="danger")


def btn_copy(text: str, value: str, style: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, copy_text=CopyTextButton(text=value), style=style)


def format_tx_line(tx: JournalEntry, unit: str = "USDT", tz_offset: int = 8) -> str:
    ts = fmt_local_dt(tx.created_at, tz_offset)
    note = f"  ·  {esc(tx.note)}" if tx.note else ""
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
    line = f"<code>#{tx.id}</code>  <code>{ts}</code>  {op}{note}  →  <code>{fmt_money(tx.balance_after, unit)}</code>"
    return f"<s>{line}</s>" if tx.voided_by_id else line


def render_home_view(d: DashboardStats, unit: str = "USDT") -> tuple[str, InlineKeyboardMarkup]:
    net = d.positive_sum + d.debt_sum
    net_sign = "+" if net > 0 else ""
    text = (
        "<b>LEDGERFLOW</b>  ·  <i>财务总控台</i>\n\n"
        "<blockquote><b>资产总览</b>\n"
        f"存款总额　<code>+{fmt_money(d.positive_sum, unit)}</code>  ({d.positive_count}人)\n"
        f"欠款总额　<code>{fmt_money(d.debt_sum, unit)}</code>  ({d.debt_count}人)\n"
        f"资金净值　<b><code>{net_sign}{fmt_money(net, unit)}</code></b></blockquote>\n\n"
        "<blockquote><b>今日动态</b>\n"
        f"入账 <code>+{fmt_money(d.today_in, unit)}</code>  ·  出账 <code>-{fmt_money(d.today_out, unit)}</code>  ·  <code>{d.today_tx_count}</code> 笔\n"
        f"客户账本 <code>{d.total_contacts}</code>（零余额 <code>{d.zero_count}</code>）  ·  待发回执 <code>{d.pending_receipts}</code></blockquote>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                btn_success(f"存款 ({d.positive_count})", "lst:positive:0"),
                btn_danger(f"欠款 ({d.debt_count})", "lst:debt:0"),
                btn_primary(f"全部 ({d.total_contacts})", "lst:all:0"),
            ],
            [
                btn("今日活跃", "lst:today:0"),
                btn("标签筛选", "tags:menu"),
                btn("搜索客户", "search"),
            ],
            [
                btn_primary("财务报表", "rep:today"),
                btn_success("导出表格", "exp:menu"),
            ],
            [
                btn("定向通知", "ann:menu"),
                btn("系统维护", "cfg:menu"),
                btn("刷新", "home"),
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
) -> tuple[str, InlineKeyboardMarkup]:
    titles = {
        "all": "全部客户",
        "positive": "存款排行",
        "debt": "欠款排行",
        "today": "今日活跃",
        "archived": "已归档客户",
    }
    title = f"标签 #{mode[4:]}" if mode.startswith("tag:") else titles.get(mode, "客户列表")
    pages = max(1, (total + page_size - 1) // page_size)
    header = (
        f"<b>{esc(title)}</b>  ·  第 <code>{page + 1}/{pages}</code> 页\n"
        f"<blockquote>共 <b>{total}</b> 位客户，点击下方条目查看详情与调账。</blockquote>"
    )
    rows: list[list[InlineKeyboardButton]] = []
    for c in items:
        sign = "+" if c.balance_tenths > 0 else ""
        arch = " [归档]" if c.archived else ""
        label = f"{c.label}{arch}  ·  {sign}{fmt_money(c.balance_tenths, unit)}"
        rows.append([btn(label, f"ct:{c.chat_id}")])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(btn("← 上一页", f"lst:{mode}:{page - 1}"))
    if (page + 1) * page_size < total:
        nav.append(btn("下一页 →", f"lst:{mode}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([btn("返回首页", "home")])
    return (header, InlineKeyboardMarkup(inline_keyboard=rows))


def render_search_view(
    query: str,
    items: list[Contact],
    total: int,
    page: int,
    page_size: int,
    unit: str = "USDT",
) -> tuple[str, InlineKeyboardMarkup]:
    pages = max(1, (total + page_size - 1) // page_size)
    header = (
        f"<b>客户搜索</b>  ·  「<code>{esc(query)}</code>」\n"
        f"<blockquote>匹配到 <b>{total}</b> 位客户（第 <code>{page + 1}/{pages}</code> 页）</blockquote>"
    )
    rows: list[list[InlineKeyboardButton]] = []
    for c in items:
        sign = "+" if c.balance_tenths > 0 else ""
        rows.append([btn(f"{c.label}  ·  {sign}{fmt_money(c.balance_tenths, unit)}", f"ct:{c.chat_id}")])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(btn("← 上一页", f"spage:{page - 1}"))
    if (page + 1) * page_size < total:
        nav.append(btn("下一页 →", f"spage:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([btn_primary("重新搜索", "search"), btn("返回首页", "home")])
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
    arch_str = "  ·  <i>已归档</i>" if c.archived else ""
    bal_sign = "+" if c.balance_tenths > 0 else ""

    parts = [
        f"<b>{esc(c.label)}</b>{arch_str}",
        (
            "<blockquote><b>客户档案</b>\n"
            f"TG 名称　{esc(c.tg_display_name)}（{uname}）\n"
            f"数字 ID　<code>{c.chat_id}</code>\n"
            f"标签组　{tags_str}</blockquote>"
        ),
        (
            "<blockquote><b>账本余额</b>\n"
            f"当前余额　<b><code>{bal_sign}{fmt_money(c.balance_tenths, unit)}</code></b>\n"
            f"累计入账　<code>+{fmt_money(c.total_in_tenths, unit)}</code>\n"
            f"累计出账　<code>-{fmt_money(c.total_out_tenths, unit)}</code>\n"
            f"流水笔数　<code>{c.tx_count}</code> 笔</blockquote>"
        ),
    ]
    if recent_txs:
        tx_lines = "\n".join(format_tx_line(tx, unit, tz_offset) for tx in recent_txs)
        parts.append(f"<blockquote expandable><b>最近 {len(recent_txs)} 笔流水（点击展开）</b>\n{tx_lines}</blockquote>")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                btn_primary("后台调账", f"adj:{c.chat_id}"),
                btn("撤销上笔", f"undo:{c.chat_id}"),
                btn_danger("结清归零", f"stl_ask:{c.chat_id}"),
            ],
            [
                btn("全部流水", f"hist:{c.chat_id}:0"),
                btn_success("导出对账单", f"exp_ct:{c.chat_id}"),
            ],
            [
                btn("设别名", f"alias:{c.chat_id}"),
                btn("设标签", f"tagset:{c.chat_id}"),
                btn_copy("复制 ID", str(c.chat_id)),
            ],
            [
                btn("定位聊天", f"loc:{c.chat_id}"),
                btn("取消归档" if c.archived else "归档客户", f"arch:{c.chat_id}"),
            ],
            [
                btn("← 返回列表", back_cb),
                btn("首页", "home"),
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
        f"<b>{name}</b>  ·  流水明细（第 <code>{page + 1}/{pages}</code> 页，共 <code>{total}</code> 笔）\n\n"
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
    rows.append([btn_success("导出 CSV", f"exp_ct:{chat_id}"), btn("← 返回客户", f"ct:{chat_id}")])
    return (text, InlineKeyboardMarkup(inline_keyboard=rows))


def render_report_view(rep: FinancialReport, unit: str = "USDT") -> tuple[str, InlineKeyboardMarkup]:
    labels = {"today": "今日", "week": "本周", "month": "本月", "all": "全部历史"}
    period = rep.period
    net_sign = "+" if rep.net_flow >= 0 else ""

    blocks = [
        f"<b>财务报表</b>  ·  <i>{labels[period]}</i>",
        (
            "<blockquote><b>收支汇总</b>\n"
            f"总入账　<code>+{fmt_money(rep.total_in, unit)}</code>\n"
            f"总出账　<code>-{fmt_money(rep.total_out, unit)}</code>\n"
            f"净流入　<b><code>{net_sign}{fmt_money(rep.net_flow, unit)}</code></b>\n"
            f"有效笔数 <code>{rep.tx_count}</code> 笔  ·  活跃客户 <code>{rep.active_contacts}</code> 人</blockquote>"
        ),
    ]
    if rep.top_positive:
        lines = "\n".join(f"{i}. {esc(c.label)}　<code>+{fmt_money(c.balance_tenths, unit)}</code>" for i, c in enumerate(rep.top_positive, 1))
        blocks.append(f"<blockquote expandable><b>存款 Top 5（点击展开）</b>\n{lines}</blockquote>")
    if rep.top_debt:
        lines = "\n".join(f"{i}. {esc(c.label)}　<code>{fmt_money(c.balance_tenths, unit)}</code>" for i, c in enumerate(rep.top_debt, 1))
        blocks.append(f"<blockquote expandable><b>欠款 Top 5（点击展开）</b>\n{lines}</blockquote>")

    def pbtn(key: str, text: str) -> InlineKeyboardButton:
        return btn_primary(text, f"rep:{key}") if period == key else btn(text, f"rep:{key}")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [pbtn("today", "今日"), pbtn("week", "本周"), pbtn("month", "本月"), pbtn("all", "全部")],
            [btn_success("导出 Excel 报表", "exp:menu"), btn("返回首页", "home")],
        ]
    )
    return ("\n\n".join(blocks), kb)


def render_settings_view(d: DashboardStats, db_ok: bool, fk_errors: int) -> tuple[str, InlineKeyboardMarkup]:
    health = "正常" if (db_ok and fk_errors == 0) else f"异常（外键错误 {fk_errors}）"
    text = (
        "<b>系统维护与设置</b>\n\n"
        "<blockquote><b>运行状态</b>\n"
        f"数据库体检　<code>{health}</code>\n"
        f"待发回执数　<code>{d.pending_receipts}</code> 条\n"
        f"回执自动置顶　<code>{'已开启' if d.auto_pin else '已关闭'}</code></blockquote>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [btn_primary(f"自动置顶：{'开' if d.auto_pin else '关'}（点击切换）", "cfg:pin")],
            [
                btn(f"待发回执 ({d.pending_receipts})", "rcpt:0"),
                btn("立即重试回执", "rcpt_retry"),
            ],
            [
                btn_success("立即热备份账本", "cfg:backup"),
                btn("已归档客户", "lst:archived:0"),
            ],
            [btn("返回首页", "home")],
        ]
    )
    return (text, kb)


def render_receipts_view(items: list[ReceiptItem], total: int) -> tuple[str, InlineKeyboardMarkup]:
    if not items:
        body = "当前队列为空，所有回执均已送达。"
    else:
        body = "\n".join(
            f"<code>#{r.id}</code> 客户 <code>{r.chat_id}</code> · 尝试 <code>{r.attempts}</code> 次"
            + (f" · {esc(r.last_error[:36])}" if r.last_error else "")
            for r in items
        )
    text = f"<b>待发回执队列</b>（共 <code>{total}</code> 条）\n\n<blockquote>{body}</blockquote>"
    rows: list[list[InlineKeyboardButton]] = []
    if total > 0:
        rows.append([btn_primary("立即重试全部", "rcpt_retry")])
    rows.append([btn("← 返回维护", "cfg:menu"), btn("首页", "home")])
    return (text, InlineKeyboardMarkup(inline_keyboard=rows))


def render_announce_status_view(
    ann_id: int,
    status: str,
    target_desc: str,
    counts: dict[str, int],
    allow_new: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"<b>定向通知 #{ann_id}</b>  ·  <code>{status}</code>\n\n"
        "<blockquote><b>投递进度</b>\n"
        f"目标范围　<code>{esc(target_desc)}</code>\n"
        f"等待 <code>{counts['pending']}</code>  ·  发送中 <code>{counts['sending']}</code>  ·  已发 <code>{counts['sent']}</code>\n"
        f"失败 <code>{counts['failed']}</code>  ·  不确定 <code>{counts['uncertain']}</code>  ·  已取消 <code>{counts['cancelled']}</code></blockquote>"
    )
    rows: list[list[InlineKeyboardButton]] = []
    if status == "running":
        rows.append([btn("暂停发送", f"ann:pause:{ann_id}"), btn_danger("取消任务", f"ann:cancel:{ann_id}")])
    elif status == "paused":
        rows.append([btn_primary("继续发送", f"ann:resume:{ann_id}"), btn_danger("取消任务", f"ann:cancel:{ann_id}")])
    if allow_new:
        rows.append([btn_primary("新建定向通知", "ann:new")])
    rows.append([btn("刷新进度", "ann:menu"), btn("返回首页", "home")])
    return (text, InlineKeyboardMarkup(inline_keyboard=rows))


def render_announce_picker_view(tags: list[str]) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "<b>新建定向通知</b>\n\n"
        "<blockquote>请选择本次通知的目标客户范围（静默发送，1.2 秒/条，不影响记账回执）。</blockquote>"
    )
    rows = [
        [btn_primary("全部有账本客户", "ann:pick:all")],
        [btn_success("存款客户（余额 > 0）", "ann:pick:positive"), btn("非负余额（含零）", "ann:pick:non_negative")],
        [btn_danger("欠款客户（余额 < 0）", "ann:pick:debt")],
    ]
    for i in range(0, min(len(tags), 6), 2):
        row = [btn(f"标签 #{t}", f"ann:pick:tag:{t}") for t in tags[i : i + 2]]
        rows.append(row)
    rows.append([btn("返回首页", "home")])
    return (text, InlineKeyboardMarkup(inline_keyboard=rows))
