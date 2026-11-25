"""aiogram 3 private-chat admin console: dashboard, reports, exports, contact CRM, announcements."""

from __future__ import annotations

import logging
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ledgerflow.domain import esc, fmt_local_dt, fmt_money, parse_chat_intent
from ledgerflow.export import export_contacts_csv, export_journal_csv
from ledgerflow.repository import Contact, LedgerRepository
from ledgerflow.workers import AnnouncementDispatcher, ReceiptDispatcher

logger = logging.getLogger(__name__)

PAGE_SIZE = 10
CONFIRM_TTL = 300.0
MAX_TOKENS = 16
MAX_ANNOUNCE_UTF16 = 2000
MAX_ANNOUNCE_TARGETS = 1000


@dataclass(frozen=True)
class SettleToken:
    owner_id: int
    chat_id: int
    expected_balance: int
    expected_tx_count: int
    created_at: float


@dataclass
class PendingPrompt:
    kind: str  # "search" | "alias" | "tags" | "adjust" | "announce_body"
    target_chat_id: int = 0
    extra: str = ""


@dataclass
class AnnounceDraft:
    target_key: str
    target_label: str
    body_text: str
    chat_ids: list[int]
    created_at: float


def _utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def create_admin_router(
    repo: LedgerRepository,
    receipts: ReceiptDispatcher,
    announcer: AnnouncementDispatcher,
    backup_dir: Path,
    tz_offset: int = 8,
) -> Router:
    router = Router(name="admin")
    prompts: dict[int, PendingPrompt] = {}
    last_search: dict[int, str] = {}
    settle_tokens: OrderedDict[str, SettleToken] = OrderedDict()
    ann_drafts: dict[int, AnnounceDraft] = {}

    def prune_tokens() -> None:
        now = time.monotonic()
        for k in [k for k, v in settle_tokens.items() if now - v.created_at > CONFIRM_TTL]:
            settle_tokens.pop(k, None)
        while len(settle_tokens) > MAX_TOKENS:
            settle_tokens.popitem(last=False)

    def render_home(owner_id: int) -> tuple[str, InlineKeyboardMarkup]:
        d = repo.get_dashboard(owner_id, tz_offset)
        u = repo.currency_unit
        net = d.positive_sum + d.debt_sum
        lines = [
            "⚡ <b>LedgerFlow 财务总控台</b>",
            f"👥 客户账本：<b>{d.total_contacts}</b>（零余额 {d.zero_count}）",
            f"🟢 存款：{d.positive_count} 人 · <b>{fmt_money(d.positive_sum, u)}</b>",
            f"🔴 欠款：{d.debt_count} 人 · <b>{fmt_money(d.debt_sum, u)}</b>",
            f"💎 资金净敞口：<b>{fmt_money(net, u)}</b>",
            "────────────────",
            f"📅 今日交易：<b>{d.today_tx_count}</b> 笔",
            f"   入账 +{fmt_money(d.today_in, u)}  |  出账 -{fmt_money(d.today_out, u)}",
            f"📨 待发回执：{d.pending_receipts}  |  自动置顶：{'开' if d.auto_pin else '关'}",
        ]
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text=f"全部 ({d.total_contacts})", callback_data="lst:all:0"),
                    InlineKeyboardButton(text=f"存款 ({d.positive_count})", callback_data="lst:positive:0"),
                    InlineKeyboardButton(text=f"欠款 ({d.debt_count})", callback_data="lst:debt:0"),
                ],
                [
                    InlineKeyboardButton(text="今日活跃", callback_data="lst:today:0"),
                    InlineKeyboardButton(text="标签筛选", callback_data="tags:menu"),
                    InlineKeyboardButton(text="搜索客户", callback_data="search"),
                ],
                [
                    InlineKeyboardButton(text="📊 财务报表", callback_data="rep:today"),
                    InlineKeyboardButton(text="📥 导出 Excel", callback_data="exp:menu"),
                ],
                [
                    InlineKeyboardButton(text="📢 定向通知", callback_data="ann:menu"),
                    InlineKeyboardButton(text="⚙️ 设置与备份", callback_data="cfg:menu"),
                ],
                [InlineKeyboardButton(text="🔄 刷新数据", callback_data="home")],
            ]
        )
        return ("\n".join(lines), kb)

    def render_contact_list(owner_id: int, mode: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
        titles = {
            "all": "全部客户",
            "positive": "存款排行",
            "debt": "欠款排行",
            "today": "今日活跃客户",
            "archived": "已归档客户",
        }
        title = f"标签 #{mode[4:]}" if mode.startswith("tag:") else titles.get(mode, "客户列表")
        items, total = repo.list_contacts(owner_id, mode, limit=PAGE_SIZE, offset=page * PAGE_SIZE, tz_offset_hours=tz_offset)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        lines = [f"📋 <b>{esc(title)}</b>（共 {total} 人 · 第 {page + 1}/{pages} 页）"]
        rows: list[list[InlineKeyboardButton]] = []
        for c in items:
            badge = "📦" if c.archived else ("🟢" if c.balance_tenths > 0 else ("🔴" if c.balance_tenths < 0 else "⚪"))
            rows.append([InlineKeyboardButton(text=f"{badge} {c.label} | {fmt_money(c.balance_tenths, repo.currency_unit)}", callback_data=f"ct:{c.chat_id}")])
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅ 上一页", callback_data=f"lst:{mode}:{page - 1}"))
        if (page + 1) * PAGE_SIZE < total:
            nav.append(InlineKeyboardButton(text="下一页 ➡", callback_data=f"lst:{mode}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton(text="🏠 返回首页", callback_data="home")])
        return ("\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))

    def render_search(owner_id: int, q: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
        items, total = repo.search_contacts(owner_id, q, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        lines = [f"🔍 搜索「<b>{esc(q)}</b>」（共 {total} 人 · 第 {page + 1}/{pages} 页）"]
        rows: list[list[InlineKeyboardButton]] = []
        for c in items:
            rows.append([InlineKeyboardButton(text=f"{c.label} | {fmt_money(c.balance_tenths, repo.currency_unit)}", callback_data=f"ct:{c.chat_id}")])
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅ 上一页", callback_data=f"spage:{page - 1}"))
        if (page + 1) * PAGE_SIZE < total:
            nav.append(InlineKeyboardButton(text="下一页 ➡", callback_data=f"spage:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton(text="🏠 返回首页", callback_data="home")])
        return ("\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))

    def render_contact_card(owner_id: int, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
        c = repo.get_contact(owner_id, chat_id)
        if not c:
            return ("客户不存在", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 返回首页", callback_data="home")]]))
        u = repo.currency_unit
        tags_str = " ".join(f"<code>#{esc(t)}</code>" for t in c.tags) if c.tags else "无"
        uname = f"@{esc(c.tg_username)}" if c.tg_username else "无"
        lines = [
            f"👤 <b>{esc(c.label)}</b>" + ("  [已归档]" if c.archived else ""),
            f"TG 名称：{esc(c.tg_display_name)} | 用户名：{uname}",
            f"数字 ID：<code>{c.chat_id}</code>",
            f"标签：{tags_str}",
            "────────────────",
            f"💰 当前余额：<b>{fmt_money(c.balance_tenths, u)}</b>",
            f"📈 累计入账：{fmt_money(c.total_in_tenths, u)}",
            f"📉 累计出账：{fmt_money(c.total_out_tenths, u)}",
            f"🧾 流水笔数：{c.tx_count} 笔",
        ]
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🧾 流水明细", callback_data=f"hist:{chat_id}:0"),
                    InlineKeyboardButton(text="📄 导出对账单", callback_data=f"exp_ct:{chat_id}"),
                ],
                [
                    InlineKeyboardButton(text="✏️ 后台调账", callback_data=f"adj:{chat_id}"),
                    InlineKeyboardButton(text="↩️ 撤销上一笔", callback_data=f"undo:{chat_id}"),
                ],
                [
                    InlineKeyboardButton(text="🏷️ 设别名", callback_data=f"alias:{chat_id}"),
                    InlineKeyboardButton(text="🔖 设标签", callback_data=f"tagset:{chat_id}"),
                ],
                [
                    InlineKeyboardButton(text="📍 定位聊天", callback_data=f"loc:{chat_id}"),
                    InlineKeyboardButton(text="🧹 结清归零", callback_data=f"stl_ask:{chat_id}"),
                ],
                [
                    InlineKeyboardButton(text="取消归档" if c.archived else "归档客户", callback_data=f"arch:{chat_id}"),
                    InlineKeyboardButton(text="🏠 返回首页", callback_data="home"),
                ],
            ]
        )
        return ("\n".join(lines), kb)

    def render_history(owner_id: int, chat_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
        c = repo.get_contact(owner_id, chat_id)
        txs, total = repo.get_journal(owner_id, chat_id, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        u = repo.currency_unit
        lines = [f"🧾 <b>{esc(c.label if c else str(chat_id))} 流水</b>（共 {total} 条 · 第 {page + 1}/{pages} 页）"]
        for tx in txs:
            ts = fmt_local_dt(tx.created_at, tz_offset)
            note = f" · {esc(tx.note)}" if tx.note else ""
            if tx.kind == "in":
                op = f"🟢 +{fmt_money(tx.amount_tenths, u)}"
            elif tx.kind == "out":
                op = f"🔴 -{fmt_money(tx.amount_tenths, u)}"
            elif tx.kind == "void":
                op = f"↩️ 撤销 ({'+' if tx.delta_tenths >= 0 else ''}{fmt_money(tx.delta_tenths, u)})"
            elif tx.kind == "settle":
                op = "🧹 清账归零"
            else:
                op = f"✏️ 调账 ({'+' if tx.delta_tenths >= 0 else ''}{fmt_money(tx.delta_tenths, u)})"
            line = f"<code>#{tx.id}</code> {ts} | {op}{note} → <b>{fmt_money(tx.balance_after, u)}</b>"
            if tx.voided_by_id:
                line = f"<s>{line}</s>"
            lines.append(line)
        rows: list[list[InlineKeyboardButton]] = []
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅ 上一页", callback_data=f"hist:{chat_id}:{page - 1}"))
        if (page + 1) * PAGE_SIZE < total:
            nav.append(InlineKeyboardButton(text="下一页 ➡", callback_data=f"hist:{chat_id}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton(text="⬅ 返回客户", callback_data=f"ct:{chat_id}")])
        return ("\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))

    def render_report(owner_id: int, period: str) -> tuple[str, InlineKeyboardMarkup]:
        labels = {"today": "今日", "week": "本周", "month": "本月", "all": "全部历史"}
        rep = repo.get_financial_report(owner_id, period, tz_offset)  # type: ignore[arg-type]
        u = repo.currency_unit
        lines = [
            f"📊 <b>财务报表（{labels[period]}）</b>",
            f"🟢 总入账：<b>+{fmt_money(rep.total_in, u)}</b>",
            f"🔴 总出账：<b>-{fmt_money(rep.total_out, u)}</b>",
            f"💎 净流入：<b>{'+' if rep.net_flow >= 0 else ''}{fmt_money(rep.net_flow, u)}</b>",
            f"🧾 有效流水：{rep.tx_count} 笔 | 活跃客户：{rep.active_contacts} 人",
        ]
        if rep.top_positive:
            lines.append("\n<b>🏆 存款 Top 5：</b>")
            for idx, c in enumerate(rep.top_positive, 1):
                lines.append(f"{idx}. {esc(c.label)} — <b>{fmt_money(c.balance_tenths, u)}</b>")
        if rep.top_debt:
            lines.append("\n<b>⚠️ 欠款 Top 5：</b>")
            for idx, c in enumerate(rep.top_debt, 1):
                lines.append(f"{idx}. {esc(c.label)} — <b>{fmt_money(c.balance_tenths, u)}</b>")

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text=("• 今日 •" if period == "today" else "今日"), callback_data="rep:today"),
                    InlineKeyboardButton(text=("• 本周 •" if period == "week" else "本周"), callback_data="rep:week"),
                    InlineKeyboardButton(text=("• 本月 •" if period == "month" else "本月"), callback_data="rep:month"),
                    InlineKeyboardButton(text=("• 全部 •" if period == "all" else "全部"), callback_data="rep:all"),
                ],
                [InlineKeyboardButton(text="📥 导出 Excel 报表", callback_data="exp:menu")],
                [InlineKeyboardButton(text="🏠 返回首页", callback_data="home")],
            ]
        )
        return ("\n".join(lines), kb)

    def render_settings(owner_id: int) -> tuple[str, InlineKeyboardMarkup]:
        d = repo.get_dashboard(owner_id, tz_offset)
        ok, fk = repo.health_check()
        lines = [
            "⚙️ <b>系统设置与账本维护</b>",
            f"数据库体检：{'✅ 正常' if (ok and fk == 0) else '❌ 异常'}（外键错误 {fk}）",
            f"待发回执：{d.pending_receipts} 条",
            f"记账自动置顶回执：<b>{'开启' if d.auto_pin else '关闭'}</b>",
        ]
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"切换自动置顶（当前：{'开' if d.auto_pin else '关'}）", callback_data="cfg:pin")],
                [
                    InlineKeyboardButton(text=f"待发回执 ({d.pending_receipts})", callback_data="rcpt:0"),
                    InlineKeyboardButton(text="立即重试回执", callback_data="rcpt_retry"),
                ],
                [
                    InlineKeyboardButton(text="📦 立即热备份账本", callback_data="cfg:backup"),
                    InlineKeyboardButton(text="已归档客户", callback_data="lst:archived:0"),
                ],
                [InlineKeyboardButton(text="🏠 返回首页", callback_data="home")],
            ]
        )
        return ("\n".join(lines), kb)

    def render_announce_status(ann_id: int, status: str, target_desc: str, allow_new: bool = False) -> tuple[str, InlineKeyboardMarkup]:
        c = repo.get_announcement_counts(ann_id)
        lines = [
            f"📢 <b>定向通知 #{ann_id}</b>（状态：<code>{status}</code>）",
            f"发送范围：{esc(target_desc)}",
            f"等待：{c['pending']} | 发送中：{c['sending']} | 已发：{c['sent']}",
            f"失败：{c['failed']} | 不确定：{c['uncertain']} | 已取消：{c['cancelled']}",
        ]
        rows: list[list[InlineKeyboardButton]] = []
        if status == "running":
            rows.append([InlineKeyboardButton(text="⏸ 暂停", callback_data=f"ann:pause:{ann_id}"), InlineKeyboardButton(text="🛑 取消", callback_data=f"ann:cancel:{ann_id}")])
        elif status == "paused":
            rows.append([InlineKeyboardButton(text="▶️ 继续", callback_data=f"ann:resume:{ann_id}"), InlineKeyboardButton(text="🛑 取消", callback_data=f"ann:cancel:{ann_id}")])
        if allow_new:
            rows.append([InlineKeyboardButton(text="➕ 新建定向通知", callback_data="ann:new")])
        rows.append([InlineKeyboardButton(text="🔄 刷新", callback_data="ann:menu"), InlineKeyboardButton(text="🏠 返回首页", callback_data="home")])
        return ("\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))

    def render_announce_picker(owner_id: int) -> tuple[str, InlineKeyboardMarkup]:
        tags = repo.list_owner_tags(owner_id)
        rows = [
            [InlineKeyboardButton(text="全部有账本客户", callback_data="ann:pick:all")],
            [InlineKeyboardButton(text="存款客户（余额 > 0）", callback_data="ann:pick:positive")],
            [InlineKeyboardButton(text="非负余额客户（含零余额）", callback_data="ann:pick:non_negative")],
            [InlineKeyboardButton(text="欠款客户（余额 < 0）", callback_data="ann:pick:debt")],
        ]
        for t in tags[:6]:
            rows.append([InlineKeyboardButton(text=f"🏷️ 按标签：#{t}", callback_data=f"ann:pick:tag:{t}")])
        rows.append([InlineKeyboardButton(text="🏠 返回首页", callback_data="home")])
        return ("📢 <b>新建定向通知</b>\n请选择目标客户范围：", InlineKeyboardMarkup(inline_keyboard=rows))

    async def safe_edit(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
        await cb.answer()
        if cb.message:
            try:
                await cb.message.edit_text(text=text, parse_mode=ParseMode.HTML, reply_markup=kb)
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    raise

    async def locate_contact(bot: Bot, owner_id: int, chat_id: int) -> tuple[bool, str]:
        for link_id, enabled in repo.list_owner_links(owner_id, limit=8):
            if not enabled:
                continue
            try:
                info = await bot.get_business_connection(link_id)
                if info.user.id != owner_id or not info.is_enabled:
                    repo.upsert_link(link_id, info.user.id, bool(info.is_enabled))
                    continue
                rights = getattr(info, "rights", None)
                if rights is not None and getattr(rights, "can_reply", True) is False:
                    continue
            except (TelegramBadRequest, TelegramForbiddenError):
                repo.upsert_link(link_id, owner_id, False)
                continue
            except TelegramNetworkError:
                return (False, "网络异常，请稍后重试")
            except TelegramAPIError:
                continue

            try:
                await bot.send_message(chat_id=chat_id, text=".", business_connection_id=link_id, disable_notification=True)
                return (True, "已发送静默定位点号")
            except TelegramAPIError as exc:
                return (False, f"定位失败：{exc}")
        return (False, "未找到可用的 Business 连接")

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    @router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
    async def on_start(message: Message) -> None:
        if not message.from_user or not repo.is_owner(message.from_user.id):
            await message.answer("请先在 Telegram Business 设置中连接本机器人，再发送 /start 进入财务总控台。")
            return
        prompts.pop(message.from_user.id, None)
        text, kb = render_home(message.from_user.id)
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    @router.message(F.chat.type == ChatType.PRIVATE, F.text)
    async def on_text_input(message: Message) -> None:
        if not message.from_user or not message.text:
            return
        owner_id = message.from_user.id
        if not repo.is_owner(owner_id):
            return
        prompt = prompts.get(owner_id)
        if prompt is None:
            return

        raw = message.text.strip()
        if prompt.kind == "search":
            prompts.pop(owner_id, None)
            if not raw or raw == "@" or len(raw) > 64:
                await message.answer("搜索关键词无效（不能为空、单个 @ 或超过 64 字）。")
                return
            last_search[owner_id] = raw
            text, kb = render_search(owner_id, raw, 0)
            await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            return

        if prompt.kind == "alias":
            prompts.pop(owner_id, None)
            val = None if raw in ("清除", "删除", "-") else raw
            repo.set_alias(owner_id, prompt.target_chat_id, val)
            text, kb = render_contact_card(owner_id, prompt.target_chat_id)
            await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            return

        if prompt.kind == "tags":
            prompts.pop(owner_id, None)
            tags = [] if raw in ("清除", "清空", "-") else [t for t in raw.replace("，", ",").replace(" ", ",").split(",") if t.strip()]
            repo.set_tags(owner_id, prompt.target_chat_id, tags)
            text, kb = render_contact_card(owner_id, prompt.target_chat_id)
            await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            return

        if prompt.kind == "adjust":
            intent = parse_chat_intent(raw)
            if not intent or intent.kind not in ("in", "out"):
                await message.answer("格式错误，请输入如 <code>+50 补差</code> 或 <code>-20 扣费</code>：", parse_mode=ParseMode.HTML)
                return
            prompts.pop(owner_id, None)
            delta = intent.amount_tenths if intent.kind == "in" else -intent.amount_tenths
            repo.admin_adjust(owner_id=owner_id, chat_id=prompt.target_chat_id, delta_tenths=delta, note=intent.note or "后台调账")
            text, kb = render_contact_card(owner_id, prompt.target_chat_id)
            await message.answer(f"✅ 已调账完成。\n\n{text}", parse_mode=ParseMode.HTML, reply_markup=kb)
            return

        if prompt.kind == "announce_body":
            if not message.text.strip() or _utf16_len(message.text) > MAX_ANNOUNCE_UTF16:
                await message.answer("通知正文不能为空，且不能超过 2000 字符，请重新输入。")
                return
            prompts.pop(owner_id, None)
            targets = repo.select_announcement_targets(owner_id, prompt.extra)
            if not targets:
                await message.answer("该范围内没有可发送的客户。")
                return
            if len(targets) > MAX_ANNOUNCE_TARGETS:
                await message.answer(f"目标人数超过上限（最多 {MAX_ANNOUNCE_TARGETS} 人）。")
                return
            draft = AnnounceDraft(
                target_key=prompt.extra,
                target_label=prompt.extra,
                body_text=message.text,
                chat_ids=[c.chat_id for c in targets],
                created_at=time.monotonic(),
            )
            ann_drafts[owner_id] = draft
            top5 = "、".join(esc(c.label) for c in targets[:5])
            preview = (
                f"📢 <b>通知发送预览（5 分钟内有效）</b>\n"
                f"目标人数：<b>{len(targets)}</b> 人\n"
                f"前 5 位：{top5}\n"
                f"────────────────\n{esc(message.text)}"
            )
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="✅ 确认发送", callback_data="ann:confirm"), InlineKeyboardButton(text="取消", callback_data="home")]]
            )
            await message.answer(preview, parse_mode=ParseMode.HTML, reply_markup=kb)

    @router.callback_query()
    async def on_callback(cb: CallbackQuery, bot: Bot) -> None:
        if not cb.from_user or not cb.data or not cb.message or cb.message.chat.type != ChatType.PRIVATE:
            await cb.answer("无权操作", show_alert=True)
            return
        owner_id = cb.from_user.id
        if not repo.is_owner(owner_id):
            await cb.answer("无权操作", show_alert=True)
            return

        parts = cb.data.split(":")
        act = parts[0]

        if act == "home":
            prompts.pop(owner_id, None)
            text, kb = render_home(owner_id)
            await safe_edit(cb, text, kb)
            return

        if act == "lst" and len(parts) >= 3:
            page = max(0, int(parts[-1]))
            mode = ":".join(parts[1:-1])
            text, kb = render_contact_list(owner_id, mode, page)
            await safe_edit(cb, text, kb)
            return

        if act == "tags":
            tags = repo.list_owner_tags(owner_id)
            if not tags:
                await cb.answer("尚未给任何客户设置标签，请先进入客户详情页设置。", show_alert=True)
                return
            rows = [[InlineKeyboardButton(text=f"🏷️ #{t}", callback_data=f"lst:tag:{t}:0")] for t in tags]
            rows.append([InlineKeyboardButton(text="🏠 返回首页", callback_data="home")])
            await safe_edit(cb, "🏷️ <b>按标签筛选客户</b>", InlineKeyboardMarkup(inline_keyboard=rows))
            return

        if act == "search":
            prompts[owner_id] = PendingPrompt(kind="search")
            await safe_edit(
                cb,
                "🔍 请直接发送要搜索的<b>别名 / 标签 / 姓名 / @用户名 / ID</b>：",
                InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="取消", callback_data="home")]]),
            )
            return

        if act == "spage" and len(parts) == 2:
            q = last_search.get(owner_id)
            if not q:
                await cb.answer("搜索已过期，请重新搜索", show_alert=True)
                return
            text, kb = render_search(owner_id, q, max(0, int(parts[1])))
            await safe_edit(cb, text, kb)
            return

        if act == "ct" and len(parts) == 2:
            prompts.pop(owner_id, None)
            text, kb = render_contact_card(owner_id, int(parts[1]))
            await safe_edit(cb, text, kb)
            return

        if act == "hist" and len(parts) == 3:
            text, kb = render_history(owner_id, int(parts[1]), max(0, int(parts[2])))
            await safe_edit(cb, text, kb)
            return

        if act == "alias" and len(parts) == 2:
            cid = int(parts[1])
            prompts[owner_id] = PendingPrompt(kind="alias", target_chat_id=cid)
            await safe_edit(
                cb,
                "🏷️ 请发送该客户的<b>自定义别名</b>（发送 <code>清除</code> 可删除别名）：",
                InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="取消", callback_data=f"ct:{cid}")]]),
            )
            return

        if act == "tagset" and len(parts) == 2:
            cid = int(parts[1])
            prompts[owner_id] = PendingPrompt(kind="tags", target_chat_id=cid)
            await safe_edit(
                cb,
                "🔖 请发送客户标签，多个标签用空格或逗号分隔（例如 <code>VIP 重点催收</code>；发送 <code>清空</code> 可移除）：",
                InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="取消", callback_data=f"ct:{cid}")]]),
            )
            return

        if act == "adj" and len(parts) == 2:
            cid = int(parts[1])
            prompts[owner_id] = PendingPrompt(kind="adjust", target_chat_id=cid)
            await safe_edit(
                cb,
                "✏️ 请输入后台调账金额与备注（例如 <code>+50 补差</code> 或 <code>-20 扣费</code>；不会向客户发回执）：",
                InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="取消", callback_data=f"ct:{cid}")]]),
            )
            return

        if act == "undo" and len(parts) == 2:
            cid = int(parts[1])
            res = repo.void_last_entry(owner_id=owner_id, chat_id=cid, actor_id=owner_id)
            if res.applied:
                await cb.answer(f"已撤销上一笔，新余额 {fmt_money(res.balance_after, repo.currency_unit)}", show_alert=True)
            else:
                await cb.answer("没有可撤销的有效流水", show_alert=True)
            text, kb = render_contact_card(owner_id, cid)
            await safe_edit(cb, text, kb)
            return

        if act == "arch" and len(parts) == 2:
            cid = int(parts[1])
            c = repo.get_contact(owner_id, cid)
            if not c:
                await cb.answer("客户不存在", show_alert=True)
                return
            repo.set_archived(owner_id, cid, not c.archived)
            await cb.answer("已更新归档状态")
            text, kb = render_contact_card(owner_id, cid)
            await safe_edit(cb, text, kb)
            return

        if act == "loc" and len(parts) == 2:
            ok, msg = await locate_contact(bot, owner_id, int(parts[1]))
            await cb.answer(msg, show_alert=not ok)
            return

        if act == "stl_ask" and len(parts) == 2:
            cid = int(parts[1])
            c = repo.get_contact(owner_id, cid)
            if not c or not c.has_ledger:
                await cb.answer("账本不存在", show_alert=True)
                return
            if c.balance_tenths == 0:
                await cb.answer("当前余额已为 0", show_alert=True)
                return
            prune_tokens()
            tok = secrets.token_hex(6)
            settle_tokens[tok] = SettleToken(owner_id, cid, c.balance_tenths, c.tx_count, time.monotonic())
            await safe_edit(
                cb,
                f"🧹 确认将 <b>{esc(c.label)}</b> 余额结清归零？\n当前余额：<b>{fmt_money(c.balance_tenths, repo.currency_unit)}</b>\n<i>仅后台结清留审计，不向客户发送回执（5 分钟有效）。</i>",
                InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="✅ 确认归零", callback_data=f"stl_do:{tok}"), InlineKeyboardButton(text="取消", callback_data=f"ct:{cid}")]]
                ),
            )
            return

        if act == "stl_do" and len(parts) == 2:
            prune_tokens()
            item = settle_tokens.pop(parts[1], None)
            if not item or item.owner_id != owner_id:
                await cb.answer("确认已过期或无效", show_alert=True)
                return
            res = repo.settle_balance(
                owner_id=owner_id,
                chat_id=item.chat_id,
                actor_id=owner_id,
                expected_balance=item.expected_balance,
                expected_tx_count=item.expected_tx_count,
            )
            if res.applied:
                await cb.answer("已结清归零")
            elif res.reason == "stale":
                await cb.answer("期间账本有新变动，请重新确认", show_alert=True)
            else:
                await cb.answer("余额已为 0", show_alert=True)
            text, kb = render_contact_card(owner_id, item.chat_id)
            await safe_edit(cb, text, kb)
            return

        if act == "rep" and len(parts) == 2 and parts[1] in ("today", "week", "month", "all"):
            text, kb = render_report(owner_id, parts[1])
            await safe_edit(cb, text, kb)
            return

        if act == "exp" and len(parts) == 2:
            sub = parts[1]
            if sub == "menu":
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="📄 导出全量客户余额表 (.csv)", callback_data="exp:balances")],
                        [InlineKeyboardButton(text="🧾 导出全量流水明细 (.csv)", callback_data="exp:journal")],
                        [InlineKeyboardButton(text="🏠 返回首页", callback_data="home")],
                    ]
                )
                await safe_edit(cb, "📥 <b>Excel / CSV 报表导出中心</b>\n所有文件均带 UTF-8 BOM，Excel / 手机 WPS 直接打开不乱码。", kb)
                return
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
            if sub == "balances":
                await cb.answer("正在生成客户余额表...")
                data = export_contacts_csv(repo.list_all_contacts_for_export(owner_id), repo.currency_unit, tz_offset)
                await bot.send_document(owner_id, BufferedInputFile(data, filename=f"ledgerflow_balances_{ts}.csv"), caption="📊 全量客户余额表")
                return
            if sub == "journal":
                await cb.answer("正在生成流水明细表...")
                data = export_journal_csv(repo.get_all_journal_for_export(owner_id), repo.currency_unit, tz_offset)
                await bot.send_document(owner_id, BufferedInputFile(data, filename=f"ledgerflow_journal_{ts}.csv"), caption="🧾 全量流水明细表")
                return

        if act == "exp_ct" and len(parts) == 2:
            cid = int(parts[1])
            c = repo.get_contact(owner_id, cid)
            await cb.answer("正在生成个人对账单...")
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
            data = export_journal_csv(repo.get_all_journal_for_export(owner_id, cid), repo.currency_unit, tz_offset)
            await bot.send_document(owner_id, BufferedInputFile(data, filename=f"statement_{cid}_{ts}.csv"), caption=f"📄 {c.label if c else cid} 个人对账单")
            return

        if act == "cfg" and len(parts) == 2:
            sub = parts[1]
            if sub == "menu":
                text, kb = render_settings(owner_id)
                await safe_edit(cb, text, kb)
                return
            if sub == "pin":
                val = repo.toggle_auto_pin(owner_id)
                await cb.answer(f"自动置顶已{'开启' if val else '关闭'}")
                text, kb = render_settings(owner_id)
                await safe_edit(cb, text, kb)
                return
            if sub == "backup":
                ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                dest = backup_dir / f"ledgerflow-hot-{ts}.db"
                try:
                    repo.hot_backup(dest)
                    for old in sorted(backup_dir.glob("ledgerflow-hot-*.db"))[:-10]:
                        old.unlink(missing_ok=True)
                    await cb.answer(f"✅ 热备份完成：{dest.name}", show_alert=True)
                except Exception as exc:
                    logger.exception("Hot backup failed")
                    await cb.answer(f"备份失败：{exc}", show_alert=True)
                return

        if act == "rcpt" and len(parts) == 2:
            page = max(0, int(parts[1]))
            items, total = repo.list_pending_receipts(owner_id, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
            lines = [f"📨 <b>待发回执队列</b>（共 {total} 条）"]
            if not items:
                lines.append("当前无待发回执。")
            for r in items:
                err = f" | {esc(r.last_error[:40])}" if r.last_error else ""
                lines.append(f"<code>#{r.id}</code> 客户 {r.chat_id} | 尝试 {r.attempts} 次{err}")
            rows = [[InlineKeyboardButton(text="🔄 立即重试全部", callback_data="rcpt_retry"), InlineKeyboardButton(text="⬅ 返回设置", callback_data="cfg:menu")]]
            await safe_edit(cb, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))
            return

        if act == "rcpt_retry":
            cnt = repo.retry_pending_receipts(owner_id)
            if cnt:
                receipts.wake()
            await cb.answer(f"已触发立即重试（{cnt} 条）" if cnt else "当前无待发回执")
            text, kb = render_settings(owner_id)
            await safe_edit(cb, text, kb)
            return

        if act == "ann" and len(parts) >= 2:
            sub = parts[1]
            if sub == "menu":
                latest = repo.get_latest_announcement(owner_id)
                if latest and latest.status in ("running", "paused"):
                    text, kb = render_announce_status(latest.id, latest.status, latest.target_desc)
                    await safe_edit(cb, text, kb)
                    return
                if latest:
                    text, kb = render_announce_status(latest.id, latest.status, latest.target_desc, allow_new=True)
                    await safe_edit(cb, text, kb)
                    return
                text, kb = render_announce_picker(owner_id)
                await safe_edit(cb, text, kb)
                return
            if sub == "new":
                text, kb = render_announce_picker(owner_id)
                await safe_edit(cb, text, kb)
                return
            if sub == "pick" and len(parts) >= 3:
                tkey = ":".join(parts[2:])
                prompts[owner_id] = PendingPrompt(kind="announce_body", extra=tkey)
                await safe_edit(
                    cb,
                    f"📢 已选范围：<code>{esc(tkey)}</code>\n请直接发送要群发的纯文本内容（最多 2000 字符）：",
                    InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="取消", callback_data="home")]]),
                )
                return
            if sub == "confirm":
                draft = ann_drafts.get(owner_id)
                if not draft or (time.monotonic() - draft.created_at > CONFIRM_TTL):
                    await cb.answer("预览已过期，请重新发起", show_alert=True)
                    return
                try:
                    ann_id = repo.create_announcement(owner_id, draft.target_label, draft.body_text, draft.chat_ids)
                except RuntimeError:
                    await cb.answer("已有进行中的通知任务", show_alert=True)
                    return
                ann_drafts.pop(owner_id, None)
                announcer.wake()
                await cb.answer("通知已启动")
                text, kb = render_announce_status(ann_id, "running", draft.target_label)
                await safe_edit(cb, text, kb)
                return
            if sub in ("pause", "resume", "cancel") and len(parts) == 3:
                ann_id = int(parts[2])
                target = {"pause": "paused", "resume": "running", "cancel": "cancelled"}[sub]
                ok = repo.set_announcement_status(ann_id, owner_id, target)  # type: ignore[arg-type]
                if ok and target == "running":
                    announcer.wake()
                await cb.answer("已更新" if ok else "任务状态已变更")
                latest = repo.get_latest_announcement(owner_id)
                if latest:
                    text, kb = render_announce_status(latest.id, latest.status, latest.target_desc, allow_new=latest.status in ("done", "cancelled"))
                    await safe_edit(cb, text, kb)
                return

        await cb.answer("未知操作")

    return router
