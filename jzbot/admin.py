"""Private-chat admin dashboard, customer management, and broadcast UI."""

from __future__ import annotations

import logging
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.error import BadRequest, Forbidden, NetworkError, TelegramError, TimedOut
from telegram.ext import ContextTypes

from jzbot.broadcast import BroadcastWorker
from jzbot.money import format_local_time, format_usdt
from jzbot.outbox import OutboxWorker
from jzbot.store import CustomerSummary, Store

logger = logging.getLogger(__name__)

PAGE_SIZE = 10
ZERO_CONFIRM_TTL_SECONDS = 300.0
MAX_ZERO_TOKENS = 16
MAX_BROADCAST_UTF16 = 2000
MAX_BROADCAST_RECIPIENTS = 1000


@dataclass(frozen=True)
class ZeroToken:
    owner_id: int
    chat_id: int
    expected_balance: int
    expected_entry_count: int
    created_monotonic: float


@dataclass
class BroadcastDraft:
    scopes: set[str] = field(default_factory=set)
    message_text: str = ""
    recipient_ids: list[int] = field(default_factory=list)
    created_monotonic: float = 0.0


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class AdminHandlers:
    """Owner-only private chat admin panel."""

    def __init__(
        self,
        store: Store,
        outbox: OutboxWorker,
        broadcaster: BroadcastWorker,
        tz_offset_hours: int = 8,
        backup_dir: Path | None = None,
    ) -> None:
        self.store = store
        self.outbox = outbox
        self.broadcaster = broadcaster
        self.tz_offset_hours = tz_offset_hours
        self.backup_dir = backup_dir or (store.db_path.parent / "backups")
        self._zero_tokens: OrderedDict[str, ZeroToken] = OrderedDict()
        self._input_mode: dict[int, Literal["search", "broadcast_text"]] = {}
        self._last_search: dict[int, str] = {}
        self._broadcast_drafts: dict[int, BroadcastDraft] = {}

    def _is_authorized_private(self, update: Update) -> int | None:
        chat = update.effective_chat
        user = update.effective_user
        if not chat or not user or chat.type != ChatType.PRIVATE:
            return None
        if not self.store.is_known_owner(user.id):
            return None
        return user.id

    def _prune_zero_tokens(self) -> None:
        now = time.monotonic()
        expired = [k for k, v in self._zero_tokens.items() if now - v.created_monotonic > ZERO_CONFIRM_TTL_SECONDS]
        for k in expired:
            self._zero_tokens.pop(k, None)
        while len(self._zero_tokens) > MAX_ZERO_TOKENS:
            self._zero_tokens.popitem(last=False)

    # ------------------------------------------------------------------
    # Entrypoints
    # ------------------------------------------------------------------

    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        owner_id = self._is_authorized_private(update)
        if owner_id is None:
            if update.message and update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
                await update.message.reply_text("请先在 Telegram Business 中连接本机器人，再使用管理后台。")
            return
        self._input_mode.pop(owner_id, None)
        text, kb = self._render_home(owner_id)
        if update.message:
            await update.message.reply_text(text, reply_markup=kb)

    async def on_private_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        owner_id = self._is_authorized_private(update)
        if owner_id is None or not update.message or not update.message.text:
            return
        mode = self._input_mode.get(owner_id)
        if mode is None:
            return

        raw = update.message.text
        if mode == "search":
            self._input_mode.pop(owner_id, None)
            q = raw.strip()
            if not q or q == "@" or len(q) > 64:
                await update.message.reply_text("搜索关键词无效（不能为空、单个 @ 或超过 64 字）。")
                return
            self._last_search[owner_id] = q
            text, kb = self._render_search_results(owner_id, q, 0)
            await update.message.reply_text(text, reply_markup=kb)
            return

        if mode == "broadcast_text":
            if utf16_length(raw) > MAX_BROADCAST_UTF16 or not raw.strip():
                await update.message.reply_text("广播正文不能为空，且不能超过 2000 个 UTF-16 字符，请重新输入。")
                return
            draft = self._broadcast_drafts.get(owner_id)
            if not draft or not draft.scopes:
                self._input_mode.pop(owner_id, None)
                await update.message.reply_text("广播草稿已失效，请重新从后台进入。")
                return
            self._input_mode.pop(owner_id, None)
            recipients = self.store.select_broadcast_recipients(owner_id, draft.scopes)
            if not recipients:
                await update.message.reply_text("当前范围内没有可发送的客户。")
                return
            if len(recipients) > MAX_BROADCAST_RECIPIENTS:
                await update.message.reply_text(f"接收人数超过上限（最多 {MAX_BROADCAST_RECIPIENTS} 人），请缩小范围。")
                return
            draft.message_text = raw
            draft.recipient_ids = [c.chat_id for c in recipients]
            draft.created_monotonic = time.monotonic()
            text, kb = self._render_broadcast_preview(draft, recipients)
            await update.message.reply_text(text, reply_markup=kb)

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.data:
            return
        owner_id = self._is_authorized_private(update)
        if owner_id is None:
            await query.answer("无权操作", show_alert=True)
            return

        data = query.data
        parts = data.split(":")
        action = parts[0]

        if action == "home":
            self._input_mode.pop(owner_id, None)
            text, kb = self._render_home(owner_id)
            await self._edit(query, text, kb)
            return

        if action == "list" and len(parts) == 3:
            mode = parts[1]
            page = max(0, int(parts[2]))
            if mode not in ("all", "positive", "debt", "deposited"):
                await query.answer("未知列表")
                return
            text, kb = self._render_customer_list(owner_id, mode, page)
            await self._edit(query, text, kb)
            return

        if action == "search":
            self._input_mode[owner_id] = "search"
            await query.answer()
            await self._edit(
                query,
                "请直接发送要搜索的客户姓名、@用户名 或数字 ID：",
                InlineKeyboardMarkup([[InlineKeyboardButton("返回首页", callback_data="home")]]),
            )
            return

        if action == "cust" and len(parts) >= 2:
            chat_id = int(parts[1])
            text, kb = self._render_customer_detail(owner_id, chat_id)
            await self._edit(query, text, kb)
            return

        if action == "hist" and len(parts) == 3:
            chat_id = int(parts[1])
            page = max(0, int(parts[2]))
            text, kb = self._render_customer_history(owner_id, chat_id, page)
            await self._edit(query, text, kb)
            return

        if action == "toggle_disable" and len(parts) == 2:
            chat_id = int(parts[1])
            cust = self.store.get_customer(owner_id, chat_id)
            if not cust:
                await query.answer("客户不存在", show_alert=True)
                return
            self.store.set_customer_disabled(owner_id, chat_id, not cust.is_disabled)
            await query.answer("已更新内部状态")
            text, kb = self._render_customer_detail(owner_id, chat_id)
            await self._edit(query, text, kb)
            return

        if action == "locate" and len(parts) == 2:
            chat_id = int(parts[1])
            ok, msg = await self._locate_customer(context, owner_id, chat_id)
            await query.answer(msg, show_alert=not ok)
            return

        if action == "zero_ask" and len(parts) == 2:
            chat_id = int(parts[1])
            cust = self.store.get_customer(owner_id, chat_id)
            if not cust or not cust.has_wallet:
                await query.answer("客户账本不存在", show_alert=True)
                return
            if cust.balance_tenths == 0:
                await query.answer("当前余额已为 0", show_alert=True)
                return
            self._prune_zero_tokens()
            token = secrets.token_hex(6)
            self._zero_tokens[token] = ZeroToken(
                owner_id=owner_id,
                chat_id=chat_id,
                expected_balance=cust.balance_tenths,
                expected_entry_count=cust.entry_count,
                created_monotonic=time.monotonic(),
            )
            text = (
                f"确认将客户 {cust.display_name}（ID: {cust.chat_id}）余额归零？\n"
                f"当前余额：{format_usdt(cust.balance_tenths)}\n"
                f"说明：仅后台归零并留审计流水，不向客户发送回执或置顶。确认 5 分钟内有效。"
            )
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("确认归零", callback_data=f"zero_do:{token}"),
                        InlineKeyboardButton("取消", callback_data=f"cust:{chat_id}"),
                    ]
                ]
            )
            await self._edit(query, text, kb)
            return

        if action == "zero_do" and len(parts) == 2:
            token = parts[1]
            self._prune_zero_tokens()
            item = self._zero_tokens.pop(token, None)
            if item is None or item.owner_id != owner_id:
                await query.answer("确认已过期或无效，请重试", show_alert=True)
                return
            res = self.store.admin_zero_customer(
                owner_id=owner_id,
                chat_id=item.chat_id,
                operator_id=owner_id,
                expected_balance_tenths=item.expected_balance,
                expected_entry_count=item.expected_entry_count,
            )
            msg_map = {
                "applied": "归零成功",
                "noop": "余额已为 0，无需操作",
                "stale": "账本期间发生变动，请重新确认",
                "missing": "客户账本不存在",
            }
            await query.answer(msg_map[res], show_alert=(res != "applied"))
            text, kb = self._render_customer_detail(owner_id, item.chat_id)
            await self._edit(query, text, kb)
            return

        if action == "spage" and len(parts) == 2:
            q = self._last_search.get(owner_id)
            if not q:
                await query.answer("搜索已过期，请重新搜索", show_alert=True)
                return
            page = max(0, int(parts[1]))
            text, kb = self._render_search_results(owner_id, q, page)
            await self._edit(query, text, kb)
            return

        if action == "receipts" and len(parts) == 2:
            page = max(0, int(parts[1]))
            text, kb = self._render_receipts(owner_id, page)
            await self._edit(query, text, kb)
            return

        if action == "receipts_retry":
            cnt = self.store.retry_all_pending_receipts(owner_id)
            if cnt > 0:
                self.outbox.notify()
            await query.answer(f"已触发立即重试（{cnt} 条）" if cnt else "当前无待发回执")
            text, kb = self._render_receipts(owner_id, 0)
            await self._edit(query, text, kb)
            return

        if action == "backup":
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            dest = self.backup_dir / f"jzbot-hot-{ts}.db"
            try:
                self.store.backup_to(dest)
                # Keep latest 10 hot backups
                existing = sorted(self.backup_dir.glob("jzbot-hot-*.db"))
                for old in existing[:-10]:
                    try:
                        old.unlink()
                    except OSError:
                        pass
                await query.answer(f"备份成功：{dest.name}", show_alert=True)
            except Exception as exc:
                logger.exception("Hot backup failed")
                await query.answer(f"备份失败：{exc}", show_alert=True)
            return

        if action == "bc":
            await self._handle_broadcast_callback(query, owner_id, parts[1:])
            return

        await query.answer("未知操作")

    # ------------------------------------------------------------------
    # Broadcast Callbacks
    # ------------------------------------------------------------------

    async def _handle_broadcast_callback(self, query, owner_id: int, args: list[str]) -> None:
        sub = args[0] if args else "menu"
        if sub == "menu":
            active = self.store.get_active_broadcast(owner_id)
            if active:
                text, kb = self._render_broadcast_status(active.id, active.status)
                await self._edit(query, text, kb)
                return
            latest = self.store.get_latest_broadcast(owner_id)
            if latest:
                text, kb = self._render_broadcast_status(latest.id, latest.status, allow_new=True)
                await self._edit(query, text, kb)
                return
            draft = self._broadcast_drafts.setdefault(owner_id, BroadcastDraft())
            text, kb = self._render_broadcast_scope_picker(owner_id, draft)
            await self._edit(query, text, kb)
            return

        if sub == "new":
            draft = BroadcastDraft()
            self._broadcast_drafts[owner_id] = draft
            text, kb = self._render_broadcast_scope_picker(owner_id, draft)
            await self._edit(query, text, kb)
            return

        if sub == "toggle" and len(args) == 2:
            scope = args[1]
            if scope not in ("traded", "non_negative", "debt"):
                await query.answer("无效范围")
                return
            draft = self._broadcast_drafts.setdefault(owner_id, BroadcastDraft())
            if scope in draft.scopes:
                draft.scopes.remove(scope)
            else:
                draft.scopes.add(scope)
            text, kb = self._render_broadcast_scope_picker(owner_id, draft)
            await self._edit(query, text, kb)
            return

        if sub == "input":
            draft = self._broadcast_drafts.get(owner_id)
            if not draft or not draft.scopes:
                await query.answer("请至少选择一个发送范围", show_alert=True)
                return
            self._input_mode[owner_id] = "broadcast_text"
            await query.answer()
            await self._edit(
                query,
                "请直接发送广播正文（纯文本，最多 2000 字符）：",
                InlineKeyboardMarkup([[InlineKeyboardButton("取消", callback_data="bc:menu")]]),
            )
            return

        if sub == "confirm":
            draft = self._broadcast_drafts.get(owner_id)
            if not draft or not draft.message_text or (time.monotonic() - draft.created_monotonic > ZERO_CONFIRM_TTL_SECONDS):
                await query.answer("预览已过期，请重新发起", show_alert=True)
                return
            current_ids = [c.chat_id for c in self.store.select_broadcast_recipients(owner_id, draft.scopes)]
            if set(current_ids) != set(draft.recipient_ids):
                await query.answer("客户名单已变化，请重新预览", show_alert=True)
                return
            try:
                job_id = self.store.create_broadcast_job(owner_id, list(draft.scopes), draft.message_text, draft.recipient_ids)
            except RuntimeError:
                await query.answer("已有进行中的广播任务", show_alert=True)
                return
            self._broadcast_drafts.pop(owner_id, None)
            self.broadcaster.notify()
            await query.answer("广播已开始")
            text, kb = self._render_broadcast_status(job_id, "running")
            await self._edit(query, text, kb)
            return

        if sub in ("pause", "resume", "cancel") and len(args) == 2:
            job_id = int(args[1])
            target_status = {"pause": "paused", "resume": "running", "cancel": "cancelled"}[sub]
            ok = self.store.set_broadcast_status(job_id, owner_id, target_status)
            if ok and target_status == "running":
                self.broadcaster.notify()
            await query.answer("已更新" if ok else "任务状态已变更")
            text, kb = self._render_broadcast_status(job_id, target_status)
            await self._edit(query, text, kb)
            return

        await query.answer("未知广播操作")

    # ------------------------------------------------------------------
    # Helpers & Renderers
    # ------------------------------------------------------------------

    async def _locate_customer(self, context: ContextTypes.DEFAULT_TYPE, owner_id: int, chat_id: int) -> tuple[bool, str]:
        candidates = self.store.list_owner_connections(owner_id, limit=8)
        for conn_id, is_enabled in candidates:
            if not is_enabled:
                continue
            try:
                info = await context.bot.get_business_connection(conn_id)
                if info.user.id != owner_id or not info.is_enabled:
                    self.store.upsert_connection(conn_id, info.user.id, bool(info.is_enabled))
                    continue
                rights = getattr(info, "rights", None)
                if rights is not None and getattr(rights, "can_reply", True) is False:
                    continue
            except (BadRequest, Forbidden):
                self.store.upsert_connection(conn_id, owner_id, False)
                continue
            except (TimedOut, NetworkError):
                return (False, "网络异常，请稍后重试")
            except TelegramError:
                continue

            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=".",
                    business_connection_id=conn_id,
                    disable_notification=True,
                )
                return (True, "已发送定位点号")
            except TelegramError as exc:
                return (False, f"定位失败: {exc}")
        return (False, "未找到可用的 Business 连接")

    async def _edit(self, query, text: str, kb: InlineKeyboardMarkup) -> None:
        await query.answer()
        try:
            await query.edit_message_text(text=text, reply_markup=kb)
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    def _render_home(self, owner_id: int) -> tuple[str, InlineKeyboardMarkup]:
        ov = self.store.get_owner_overview(owner_id)
        lines = [
            "📊 记账管理后台",
            f"客户总数：{ov.total_customers}（零余额 {ov.zero_count}）",
            f"存款客户：{ov.positive_count} 人 | 合计 {format_usdt(ov.positive_sum_tenths)}",
            f"欠款客户：{ov.debt_count} 人 | 合计 {format_usdt(ov.debt_sum_tenths)}",
            f"待发回执：{ov.pending_receipts}（异常提醒 {ov.alert_receipts}）",
        ]
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(f"全部 ({ov.total_customers})", callback_data="list:all:0"),
                    InlineKeyboardButton(f"存款 ({ov.positive_count})", callback_data="list:positive:0"),
                    InlineKeyboardButton(f"欠款 ({ov.debt_count})", callback_data="list:debt:0"),
                ],
                [
                    InlineKeyboardButton("历史入金", callback_data="list:deposited:0"),
                    InlineKeyboardButton("搜索客户", callback_data="search"),
                ],
                [
                    InlineKeyboardButton(f"待发回执 ({ov.pending_receipts})", callback_data="receipts:0"),
                    InlineKeyboardButton("客户广播", callback_data="bc:menu"),
                ],
                [
                    InlineKeyboardButton("立即备份账本", callback_data="backup"),
                    InlineKeyboardButton("刷新", callback_data="home"),
                ],
            ]
        )
        return ("\n".join(lines), kb)

    def _render_customer_list(
        self,
        owner_id: int,
        mode: Literal["all", "positive", "debt", "deposited"],
        page: int,
    ) -> tuple[str, InlineKeyboardMarkup]:
        titles = {"all": "全部客户", "positive": "存款客户", "debt": "欠款客户", "deposited": "历史入金客户"}
        items, total = self.store.list_customers(owner_id, mode, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        lines = [f"📋 {titles[mode]}（共 {total} 人，第 {page + 1}/{total_pages} 页）"]
        rows: list[list[InlineKeyboardButton]] = []
        for c in items:
            flag = " [停用]" if c.is_disabled else ""
            label = f"{c.display_name}{flag} | {format_usdt(c.balance_tenths)}"
            rows.append([InlineKeyboardButton(label, callback_data=f"cust:{c.chat_id}")])

        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅ 上一页", callback_data=f"list:{mode}:{page - 1}"))
        if (page + 1) * PAGE_SIZE < total:
            nav.append(InlineKeyboardButton("下一页 ➡", callback_data=f"list:{mode}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton("返回首页", callback_data="home")])
        return ("\n".join(lines), InlineKeyboardMarkup(rows))

    def _render_search_results(self, owner_id: int, q: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
        items, total = self.store.search_customers(owner_id, q, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        lines = [f"🔍 搜索「{q}」（共 {total} 人，第 {page + 1}/{total_pages} 页）"]
        rows: list[list[InlineKeyboardButton]] = []
        for c in items:
            label = f"{c.display_name} | {format_usdt(c.balance_tenths)}"
            rows.append([InlineKeyboardButton(label, callback_data=f"cust:{c.chat_id}")])
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅ 上一页", callback_data=f"spage:{page - 1}"))
        if (page + 1) * PAGE_SIZE < total:
            nav.append(InlineKeyboardButton("下一页 ➡", callback_data=f"spage:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton("返回首页", callback_data="home")])
        return ("\n".join(lines), InlineKeyboardMarkup(rows))

    def _render_customer_detail(self, owner_id: int, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
        c = self.store.get_customer(owner_id, chat_id)
        if not c:
            return ("客户不存在", InlineKeyboardMarkup([[InlineKeyboardButton("返回首页", callback_data="home")]]))
        status_label = "已停用（内部标记）" if c.is_disabled else "正常"
        uname = f"@{c.username}" if c.username else "无"
        lines = [
            f"👤 客户详情：{c.display_name}",
            f"ID：{c.chat_id} | 用户名：{uname}",
            f"状态：{status_label}",
            f"当前余额：{format_usdt(c.balance_tenths)}",
            f"累计入金：{format_usdt(c.deposit_total_tenths)}",
            f"累计出金：{format_usdt(c.withdraw_total_tenths)}",
            f"流水笔数：{c.entry_count}",
        ]
        toggle_label = "恢复启用" if c.is_disabled else "内部停用"
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("查看流水", callback_data=f"hist:{chat_id}:0"),
                    InlineKeyboardButton("定位聊天", callback_data=f"locate:{chat_id}"),
                ],
                [
                    InlineKeyboardButton("管理员归零", callback_data=f"zero_ask:{chat_id}"),
                    InlineKeyboardButton(toggle_label, callback_data=f"toggle_disable:{chat_id}"),
                ],
                [InlineKeyboardButton("返回首页", callback_data="home")],
            ]
        )
        return ("\n".join(lines), kb)

    def _render_customer_history(self, owner_id: int, chat_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
        c = self.store.get_customer(owner_id, chat_id)
        txs, total = self.store.get_recent_transactions(owner_id, chat_id, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        name = c.display_name if c else str(chat_id)
        lines = [f"🧾 {name} 流水明细（共 {total} 条，第 {page + 1}/{total_pages} 页）"]
        for tx in txs:
            ts = format_local_time(tx.created_at, self.tz_offset_hours)
            if tx.kind == "deposit":
                op = f"入金 +{format_usdt(tx.amount_tenths)}"
            elif tx.kind == "withdraw":
                op = f"出金 -{format_usdt(tx.amount_tenths)}"
            elif tx.kind == "clear":
                op = "账本已清空"
            else:
                op = "管理员归零"
            lines.append(f"{ts} | {op} → 余额 {format_usdt(tx.balance_after)}")
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅ 上一页", callback_data=f"hist:{chat_id}:{page - 1}"))
        if (page + 1) * PAGE_SIZE < total:
            nav.append(InlineKeyboardButton("下一页 ➡", callback_data=f"hist:{chat_id}:{page + 1}"))
        rows: list[list[InlineKeyboardButton]] = []
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton("返回客户", callback_data=f"cust:{chat_id}")])
        return ("\n".join(lines), InlineKeyboardMarkup(rows))

    def _render_receipts(self, owner_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
        items, total = self.store.list_pending_receipts(owner_id, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        lines = [f"📨 待发回执队列（共 {total} 条，第 {page + 1}/{total_pages} 页）"]
        if not items:
            lines.append("当前无待发回执。")
        for r in items:
            err = f" | {r.last_error[:40]}" if r.last_error else ""
            lines.append(f"#{r.id} 客户 {r.chat_id} | 尝试 {r.attempt_count} 次 | {r.receipt_text}{err}")
        rows: list[list[InlineKeyboardButton]] = []
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅ 上一页", callback_data=f"receipts:{page - 1}"))
        if (page + 1) * PAGE_SIZE < total:
            nav.append(InlineKeyboardButton("下一页 ➡", callback_data=f"receipts:{page + 1}"))
        if nav:
            rows.append(nav)
        if total > 0:
            rows.append([InlineKeyboardButton("立即重试全部", callback_data="receipts_retry")])
        rows.append([InlineKeyboardButton("刷新", callback_data=f"receipts:{page}"), InlineKeyboardButton("返回首页", callback_data="home")])
        return ("\n".join(lines), InlineKeyboardMarkup(rows))

    def _render_broadcast_scope_picker(self, owner_id: int, draft: BroadcastDraft) -> tuple[str, InlineKeyboardMarkup]:
        count = len(self.store.select_broadcast_recipients(owner_id, draft.scopes)) if draft.scopes else 0
        def mark(key: str, label: str) -> str:
            return f"✅ {label}" if key in draft.scopes else f"⬜ {label}"
        lines = [
            "📢 新建客户广播",
            "请选择接收范围（可多选，含内部标记客户）：",
            f"当前预计人数：{count}",
        ]
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(mark("traded", "有交易记录"), callback_data="bc:toggle:traded")],
                [InlineKeyboardButton(mark("non_negative", "存款（余额 ≥ 0，含零余额）"), callback_data="bc:toggle:non_negative")],
                [InlineKeyboardButton(mark("debt", "欠款（余额 < 0）"), callback_data="bc:toggle:debt")],
                [InlineKeyboardButton("下一步：输入正文", callback_data="bc:input")],
                [InlineKeyboardButton("返回首页", callback_data="home")],
            ]
        )
        return ("\n".join(lines), kb)

    def _render_broadcast_preview(self, draft: BroadcastDraft, recipients: list[CustomerSummary]) -> tuple[str, InlineKeyboardMarkup]:
        top5 = "、".join(c.display_name for c in recipients[:5])
        lines = [
            "📢 广播发送前预览（5 分钟内有效）",
            f"去重人数：{len(recipients)}",
            f"前 5 名客户：{top5}",
            "--- 正文 ---",
            draft.message_text,
        ]
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("确认发送", callback_data="bc:confirm"),
                    InlineKeyboardButton("取消", callback_data="home"),
                ]
            ]
        )
        return ("\n".join(lines), kb)

    def _render_broadcast_status(self, job_id: int, status: str, allow_new: bool = False) -> tuple[str, InlineKeyboardMarkup]:
        counts = self.store.get_broadcast_counts(job_id)
        lines = [
            f"📢 广播任务 #{job_id}（状态：{status}）",
            f"等待：{counts['pending']} | 发送中：{counts['sending']} | 已发：{counts['sent']}",
            f"失败：{counts['failed']} | 不确定：{counts['uncertain']} | 已取消：{counts['cancelled']}",
        ]
        controls: list[InlineKeyboardButton] = []
        if status == "running":
            controls.append(InlineKeyboardButton("暂停", callback_data=f"bc:pause:{job_id}"))
            controls.append(InlineKeyboardButton("取消", callback_data=f"bc:cancel:{job_id}"))
        elif status == "paused":
            controls.append(InlineKeyboardButton("继续", callback_data=f"bc:resume:{job_id}"))
            controls.append(InlineKeyboardButton("取消", callback_data=f"bc:cancel:{job_id}"))
        rows: list[list[InlineKeyboardButton]] = []
        if controls:
            rows.append(controls)
        if allow_new:
            rows.append([InlineKeyboardButton("新建广播", callback_data="bc:new")])
        rows.append([InlineKeyboardButton("刷新", callback_data="bc:menu"), InlineKeyboardButton("返回首页", callback_data="home")])
        return ("\n".join(lines), InlineKeyboardMarkup(rows))
