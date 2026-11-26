"""aiogram 3 private-chat admin console powered by the LedgerFlow private-bank UI."""

from __future__ import annotations

import asyncio
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
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from ledgerflow.domain import esc, fmt_money, parse_chat_intent
from ledgerflow.emojis import ANNOUNCE, NO, OK, SEARCH, TAG, tg_emoji
from ledgerflow.repository import LedgerRepository
from ledgerflow.ui import (
    btn,
    btn_danger,
    btn_success,
    render_announce_picker_view,
    render_announce_status_view,
    render_contact_detail_view,
    render_contact_list_view,
    render_history_view,
    render_home_view,
    render_receipts_view,
    render_report_view,
    render_search_view,
    render_settings_view,
    strip_button_icons,
)
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
    last_list_cb: dict[int, str] = {}
    settle_tokens: OrderedDict[str, SettleToken] = OrderedDict()
    ann_drafts: dict[int, AnnounceDraft] = {}

    def prune_tokens() -> None:
        now = time.monotonic()
        for k in [k for k, v in settle_tokens.items() if now - v.created_at > CONFIRM_TTL]:
            settle_tokens.pop(k, None)
        while len(settle_tokens) > MAX_TOKENS:
            settle_tokens.popitem(last=False)

    def render_home(owner_id: int) -> tuple[str, InlineKeyboardMarkup]:
        return render_home_view(repo.get_dashboard(owner_id, tz_offset), repo.currency_unit)

    def render_contact_card(owner_id: int, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
        c = repo.get_contact(owner_id, chat_id)
        if not c:
            return ("<b>客户不存在</b>", InlineKeyboardMarkup(inline_keyboard=[[btn("返回首页", "home")]]))
        recent, _ = repo.get_journal(owner_id, chat_id, limit=3, offset=0)
        back_cb = last_list_cb.get(owner_id, "lst:all:0")
        return render_contact_detail_view(c, recent, back_cb, repo.currency_unit, tz_offset)

    def _is_icon_rejected(exc: TelegramBadRequest) -> bool:
        msg = str(exc).lower()
        return any(k in msg for k in ("icon", "emoji", "premium", "forbidden"))

    async def send_html(message: Message, text: str, kb: InlineKeyboardMarkup) -> None:
        try:
            await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except TelegramBadRequest as exc:
            if _is_icon_rejected(exc):
                logger.warning("Button custom emoji rejected on send, falling back: %s", exc)
                await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=strip_button_icons(kb))
            else:
                raise

    async def safe_edit(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup, *, answer: bool = True) -> None:
        if answer:
            await cb.answer()
        if cb.message:
            try:
                await cb.message.edit_text(text=text, parse_mode=ParseMode.HTML, reply_markup=kb)
            except TelegramBadRequest as exc:
                low = str(exc).lower()
                if "message is not modified" in low:
                    return
                if _is_icon_rejected(exc):
                    logger.warning("Button custom emoji rejected on edit, falling back: %s", exc)
                    try:
                        await cb.message.edit_text(text=text, parse_mode=ParseMode.HTML, reply_markup=strip_button_icons(kb))
                    except TelegramBadRequest as exc2:
                        if "message is not modified" not in str(exc2).lower():
                            raise
                    return
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
        await send_html(message, text, kb)

    @router.message(F.chat.type == ChatType.PRIVATE, F.text)
    async def on_text_input(message: Message) -> None:
        if not message.from_user or not message.text:
            return
        owner_id = message.from_user.id
        if not repo.is_owner(owner_id):
            return
        raw = message.text.strip()
        if prompt is None:
            if raw.startswith("/") or not raw or raw == "@" or len(raw) > 64:
                return
            last_search[owner_id] = raw
            last_list_cb[owner_id] = "spage:0"
            items, total = repo.search_contacts(owner_id, raw, limit=PAGE_SIZE, offset=0)
            text, kb = render_search_view(raw, items, total, 0, PAGE_SIZE, repo.currency_unit)
            await send_html(message, text, kb)
            return

        if prompt.kind == "search":
            prompts.pop(owner_id, None)
            if not raw or raw == "@" or len(raw) > 64:
                await message.answer("搜索关键词无效（不能为空、单个 @ 或超过 64 字）。")
                return
            last_search[owner_id] = raw
            last_list_cb[owner_id] = "spage:0"
            items, total = repo.search_contacts(owner_id, raw, limit=PAGE_SIZE, offset=0)
            text, kb = render_search_view(raw, items, total, 0, PAGE_SIZE, repo.currency_unit)
            await send_html(message, text, kb)
            return

        if prompt.kind == "alias":
            prompts.pop(owner_id, None)
            val = None if raw in ("清除", "删除", "-") else raw
            repo.set_alias(owner_id, prompt.target_chat_id, val)
            text, kb = render_contact_card(owner_id, prompt.target_chat_id)
            await send_html(message, text, kb)
            return

        if prompt.kind == "tags":
            prompts.pop(owner_id, None)
            tags = [] if raw in ("清除", "清空", "-") else [t for t in raw.replace("，", ",").replace(" ", ",").split(",") if t.strip()]
            repo.set_tags(owner_id, prompt.target_chat_id, tags)
            text, kb = render_contact_card(owner_id, prompt.target_chat_id)
            await send_html(message, text, kb)
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
            await send_html(message, f"<i>已记录后台调账</i>\n\n{text}", kb)
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
                f"{tg_emoji(ANNOUNCE)} <b>定向通知预览</b>  ·  <i>5 分钟内有效</i>\n\n"
                f"<blockquote>目标人数　<b><code>{len(targets)}</code></b> 人\n"
                f"前 5 位　{top5}</blockquote>\n\n"
                f"<blockquote><b>正文内容</b>\n{esc(message.text)}</blockquote>"
            )
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[btn_success("确认发送", "ann:confirm", icon=OK), btn_danger("取消", "home", icon=NO)]]
            )
            await send_html(message, preview, kb)

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
            last_list_cb[owner_id] = f"lst:{mode}:{page}"
            items, total, sum_bal = repo.list_contacts(owner_id, mode, limit=PAGE_SIZE, offset=page * PAGE_SIZE, tz_offset_hours=tz_offset)
            text, kb = render_contact_list_view(items, total, mode, page, PAGE_SIZE, repo.currency_unit, sum_bal)
            await safe_edit(cb, text, kb)
            return

        if act == "tags":
            tags = repo.list_owner_tags(owner_id)
            if not tags:
                await cb.answer("尚未设置任何客户标签，请先进入客户详情页添加。", show_alert=True)
                return
            rows = []
            for i in range(0, len(tags), 2):
                rows.append([btn(f"#{t}", f"lst:tag:{t}:0", icon=TAG) for t in tags[i : i + 2]])
            rows.append([btn("返回首页", "home")])
            await safe_edit(cb, f"{tg_emoji(TAG)} <b>按标签筛选客户</b>\n\n<blockquote>选择一个标签查看对应分组：</blockquote>", InlineKeyboardMarkup(inline_keyboard=rows))
            return

        if act == "search":
            prompts[owner_id] = PendingPrompt(kind="search")
            await safe_edit(
                cb,
                f"{tg_emoji(SEARCH)} <b>搜索客户</b>\n\n<blockquote>请直接发送<b>别名 / 标签 / 姓名 / @用户名 / 数字 ID</b>：</blockquote>",
                InlineKeyboardMarkup(inline_keyboard=[[btn("取消", "home")]]),
            )
            return

        if act == "spage" and len(parts) == 2:
            q = last_search.get(owner_id)
            if not q:
                await cb.answer("搜索已过期，请重新搜索", show_alert=True)
                return
            page = max(0, int(parts[1]))
            last_list_cb[owner_id] = f"spage:{page}"
            items, total = repo.search_contacts(owner_id, q, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
            text, kb = render_search_view(q, items, total, page, PAGE_SIZE, repo.currency_unit)
            await safe_edit(cb, text, kb)
            return

        if act == "ct" and len(parts) == 2:
            prompts.pop(owner_id, None)
            text, kb = render_contact_card(owner_id, int(parts[1]))
            await safe_edit(cb, text, kb)
            return

        if act == "hist" and len(parts) == 3:
            cid = int(parts[1])
            page = max(0, int(parts[2]))
            c = repo.get_contact(owner_id, cid)
            txs, total = repo.get_journal(owner_id, cid, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
            text, kb = render_history_view(c, cid, txs, total, page, PAGE_SIZE, repo.currency_unit, tz_offset)
            await safe_edit(cb, text, kb)
            return

        if act == "alias" and len(parts) == 2:
            cid = int(parts[1])
            prompts[owner_id] = PendingPrompt(kind="alias", target_chat_id=cid)
            await safe_edit(
                cb,
                "<b>设置客户别名</b>\n\n<blockquote>请直接发送自定义别名（发送 <code>清除</code> 可恢复原名）：</blockquote>",
                InlineKeyboardMarkup(inline_keyboard=[[btn("取消", f"ct:{cid}")]]),
            )
            return

        if act == "tagset" and len(parts) == 2:
            cid = int(parts[1])
            prompts[owner_id] = PendingPrompt(kind="tags", target_chat_id=cid)
            await safe_edit(
                cb,
                "<b>设置客户标签</b>\n\n<blockquote>发送标签名，多个标签用空格或逗号分隔（如 <code>VIP 重点</code>；发送 <code>清空</code> 移除）：</blockquote>",
                InlineKeyboardMarkup(inline_keyboard=[[btn("取消", f"ct:{cid}")]]),
            )
            return

        if act == "adj" and len(parts) == 2:
            cid = int(parts[1])
            prompts[owner_id] = PendingPrompt(kind="adjust", target_chat_id=cid)
            await safe_edit(
                cb,
                "<b>后台调账</b>\n\n<blockquote>点击下方快捷金额，或直接发送调账金额与备注（如 <code>+50 补差</code> / <code>-20 扣费</code>；仅内部记账不发客户回执）：</blockquote>",
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            btn_success("+10", f"qadj:{cid}:100"),
                            btn_success("+50", f"qadj:{cid}:500"),
                            btn_success("+100", f"qadj:{cid}:1000"),
                        ],
                        [
                            btn_danger("-10", f"qadj:{cid}:-100"),
                            btn_danger("-50", f"qadj:{cid}:-500"),
                            btn_danger("-100", f"qadj:{cid}:-1000"),
                        ],
                        [btn("取消", f"ct:{cid}")],
                    ]
                ),
            )
            return

        if act == "qadj" and len(parts) == 3:
            cid = int(parts[1])
            delta = int(parts[2])
            if delta == 0:
                await cb.answer("无效金额", show_alert=True)
                return
            prompts.pop(owner_id, None)
            res = repo.admin_adjust(owner_id=owner_id, chat_id=cid, delta_tenths=delta, note="后台快捷调账")
            if not res.applied:
                await cb.answer("客户不存在", show_alert=True)
                return
            await cb.answer(f"已调账 {'+' if delta > 0 else ''}{fmt_money(delta, repo.currency_unit)}")
            text, kb = render_contact_card(owner_id, cid)
            await safe_edit(cb, text, kb, answer=False)
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
            await safe_edit(cb, text, kb, answer=False)
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
                (
                    "<b>确认结清归零</b>\n\n"
                    f"<blockquote>客户　<b>{esc(c.label)}</b>\n"
                    f"当前余额　<b><code>{fmt_money(c.balance_tenths, repo.currency_unit)}</code></b>\n"
                    "<i>仅后台结清留审计记录，不向客户发送回执（5 分钟有效）。</i></blockquote>"
                ),
                InlineKeyboardMarkup(
                    inline_keyboard=[[btn_danger("确认归零", f"stl_do:{tok}"), btn("取消", f"ct:{cid}")]]
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
            await safe_edit(cb, text, kb, answer=False)
            return

        if act == "rep" and len(parts) == 2 and parts[1] in ("today", "week", "month", "all"):
            rep = repo.get_financial_report(owner_id, parts[1], tz_offset)  # type: ignore[arg-type]
            text, kb = render_report_view(rep, repo.currency_unit)
            await safe_edit(cb, text, kb)
            return

        if act == "cfg" and len(parts) == 2:
            sub = parts[1]
            if sub == "menu":
                text, kb = render_settings_view(repo.get_dashboard(owner_id, tz_offset))
                await safe_edit(cb, text, kb)
                return
            if sub == "pin":
                val = repo.toggle_auto_pin(owner_id)
                await cb.answer(f"自动置顶已{'开启' if val else '关闭'}")
                text, kb = render_settings_view(repo.get_dashboard(owner_id, tz_offset))
                await safe_edit(cb, text, kb, answer=False)
                return
            if sub == "health":
                ok, fk = await asyncio.to_thread(repo.health_check)
                msg = "数据库体检通过（quick_check=ok，外键错误=0）" if (ok and fk == 0) else f"体检异常（ok={ok}, fk={fk}）"
                await cb.answer(msg, show_alert=True)
                return
            if sub == "backup":
                ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                dest = backup_dir / f"jzbot-hot-{ts}.db"
                try:
                    await asyncio.to_thread(repo.hot_backup, dest)
                    for old in sorted(backup_dir.glob("jzbot-hot-*.db"))[:-10]:
                        old.unlink(missing_ok=True)
                    await cb.answer(f"热备份完成：{dest.name}", show_alert=True)
                except Exception as exc:
                    logger.exception("Hot backup failed")
                    await cb.answer(f"备份失败：{exc}", show_alert=True)
                return

        if act == "rcpt" and len(parts) == 2:
            page = max(0, int(parts[1]))
            items, total = repo.list_pending_receipts(owner_id, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
            text, kb = render_receipts_view(items, total, page, PAGE_SIZE)
            await safe_edit(cb, text, kb)
            return

        if act == "rcpt_retry":
            cnt = repo.retry_pending_receipts(owner_id)
            if cnt:
                receipts.wake()
            await cb.answer(f"已触发立即重试（{cnt} 条）" if cnt else "当前无待发回执")
            text, kb = render_settings_view(repo.get_dashboard(owner_id, tz_offset))
            await safe_edit(cb, text, kb, answer=False)
            return

        if act == "rcpt_del" and len(parts) == 3:
            rid = int(parts[1])
            page = max(0, int(parts[2]))
            ok = repo.discard_receipt(owner_id, rid)
            await cb.answer(f"已忽略回执 #{rid}" if ok else "回执不存在或已发送")
            items, total = repo.list_pending_receipts(owner_id, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
            if not items and page > 0:
                page -= 1
                items, total = repo.list_pending_receipts(owner_id, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
            text, kb = render_receipts_view(items, total, page, PAGE_SIZE)
            await safe_edit(cb, text, kb, answer=False)
            return

        if act == "ann" and len(parts) >= 2:
            sub = parts[1]
            if sub == "menu":
                latest = repo.get_latest_announcement(owner_id)
                if latest:
                    text, kb = render_announce_status_view(
                        latest.id,
                        latest.status,
                        latest.target_desc,
                        repo.get_announcement_counts(latest.id),
                        allow_new=latest.status in ("done", "cancelled"),
                    )
                    await safe_edit(cb, text, kb)
                    return
                text, kb = render_announce_picker_view(repo.list_owner_tags(owner_id))
                await safe_edit(cb, text, kb)
                return
            if sub == "new":
                text, kb = render_announce_picker_view(repo.list_owner_tags(owner_id))
                await safe_edit(cb, text, kb)
                return
            if sub == "pick" and len(parts) >= 3:
                tkey = ":".join(parts[2:])
                prompts[owner_id] = PendingPrompt(kind="announce_body", extra=tkey)
                await safe_edit(
                    cb,
                    f"<b>输入通知正文</b>\n\n<blockquote>目标范围：<code>{esc(tkey)}</code>\n请直接发送纯文本内容（最多 2000 字符）：</blockquote>",
                    InlineKeyboardMarkup(inline_keyboard=[[btn("取消", "home")]]),
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
                text, kb = render_announce_status_view(ann_id, "running", draft.target_label, repo.get_announcement_counts(ann_id))
                await safe_edit(cb, text, kb, answer=False)
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
                    text, kb = render_announce_status_view(
                        latest.id,
                        latest.status,
                        latest.target_desc,
                        repo.get_announcement_counts(latest.id),
                        allow_new=latest.status in ("done", "cancelled"),
                    )
                    await safe_edit(cb, text, kb, answer=False)
                return

        await cb.answer("未知操作")

    return router
