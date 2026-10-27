from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType
from telegram.error import (
    BadRequest,
    TelegramError,
)
from telegram.ext import ContextTypes

from jzbot.domain import (
    ADMIN_PAGE_SIZE,
    ADMIN_SEARCH_MAX_LENGTH,
    admin_page_bounds,
    locate_failure_message,
    normalize_admin_search,
)
from jzbot.infra.rich import classify_rich_edit_error, send_admin_rich
from jzbot.ui.pages import (
    admin_button,
    admin_navigation,
    build_admin_notice_view,
    build_admin_zero_confirmation,
    build_customer_detail_view,
    build_customer_history_view,
    build_customer_list_view,
    build_home_view,
    build_outbox_attention_view,
    build_search_results_view,
    safe_edit_query,
)

logger = logging.getLogger("jzbot")


class AdminHandlers:
    def __init__(self, store, connections, outbox, accounting, management):
        self.store = store
        self.connections = connections
        self.outbox = outbox
        self.accounting = accounting
        self.management = management

    async def get_admin_home_view(self, owner_user_id: int):
        return build_home_view(
            await self.store.get_wallet_rows(owner_user_id),
            await self.store.get_outbox_attention_count(owner_user_id),
        )

    async def get_admin_search_view(
        self, owner_user_id: int, search_query: str, page: int
    ):
        page = max(0, min(page, 999999))
        rows, total = await self.store.search_customers(
            owner_user_id, search_query, offset=max(0, page) * ADMIN_PAGE_SIZE
        )
        actual_page, _ = admin_page_bounds(total, page)
        if actual_page != page:
            rows, total = await self.store.search_customers(
                owner_user_id, search_query, offset=actual_page * ADMIN_PAGE_SIZE
            )
        return build_search_results_view(search_query, rows, total, actual_page)

    async def on_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.message
        user = update.effective_user
        chat = update.effective_chat
        if message is None or user is None or chat is None:
            return
        if chat.type != ChatType.PRIVATE:
            return
        if not await self.store.is_business_owner(user.id):
            await message.reply_text(
                "您还没有连接此 Business 机器人，暂时没有可管理的账本。"
            )
            return
        context.user_data.pop("awaiting_customer_search", None)
        context.user_data.pop("awaiting_broadcast_text", None)
        context.user_data.pop("broadcast_preview", None)
        text, keyboard = await self.get_admin_home_view(user.id)
        await send_admin_rich(context.bot, chat.id, text, keyboard)

    async def on_cancel_search(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.message
        user = update.effective_user
        chat = update.effective_chat
        if (
            message is None
            or user is None
            or chat is None
            or (chat.type != ChatType.PRIVATE)
        ):
            return
        context.user_data.pop("awaiting_customer_search", None)
        context.user_data.pop("customer_search_query", None)
        context.user_data.pop("awaiting_broadcast_text", None)
        context.user_data.pop("broadcast_preview", None)
        if not await self.store.is_business_owner(user.id):
            return
        text, keyboard = await self.get_admin_home_view(user.id)
        await send_admin_rich(context.bot, chat.id, text, keyboard)

    async def on_admin_search_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.message
        user = update.effective_user
        chat = update.effective_chat
        if (
            message is None
            or user is None
            or chat is None
            or (chat.type != ChatType.PRIVATE)
        ):
            return
        if await self.broadcast.on_text(update, context):
            return
        if not context.user_data.get("awaiting_customer_search", False):
            return
        if not await self.store.is_business_owner(user.id):
            return
        search_query = (message.text or "").strip()
        if not normalize_admin_search(search_query):
            error_text = (
                f"搜索词最多 {ADMIN_SEARCH_MAX_LENGTH} 个字符，请缩短后重试。"
                if len(search_query) > ADMIN_SEARCH_MAX_LENGTH
                else "请输入姓名、@用户名或 Telegram ID，不能只输入 @ 或空白。"
            )
            text, keyboard = build_admin_notice_view(
                "搜索客户",
                error_text,
                InlineKeyboardMarkup(
                    [[admin_button("取消", "search_cancel")], admin_navigation()]
                ),
            )
            await send_admin_rich(context.bot, chat.id, text, keyboard)
            return
        context.user_data.pop("awaiting_customer_search", None)
        context.user_data["customer_search_query"] = search_query
        rows, total = await self.store.search_customers(
            user.id, search_query, limit=ADMIN_PAGE_SIZE, offset=0
        )
        text, keyboard = build_search_results_view(search_query, rows, total, 0)
        await send_admin_rich(context.bot, chat.id, text, keyboard)

    async def on_customer_management_callback(
        self, query, user, context, data: str
    ) -> bool:
        """Only opaque confirmation nonces, never customer/amount data, are trusted."""
        if not data.startswith(("state:", "zero:", "zero_confirm:", "zero_cancel:")):
            return False
        if (
            query.message is None
            or query.inline_message_id
            or query.message.chat_id != user.id
        ):
            await query.answer("请在机器人私聊中操作自己的账本", show_alert=True)
            return True
        requests = context.user_data.setdefault("admin_zero_requests", {})
        if data.startswith(("zero_confirm:", "zero_cancel:")):
            parts = data.split(":")
            request = requests.get(parts[1]) if len(parts) == 2 else None
            if (
                request is None
                or request["owner_user_id"] != user.id
                or request["administrator_id"] != user.id
            ):
                await query.answer(
                    "确认已失效或已取消，请重新打开客户账本", show_alert=True
                )
                return True
            await query.answer()
            customer = request["customer_chat_id"]
            kind, page = (
                request.get("ui_back_kind", request["back_kind"]),
                request["back_page"],
            )
            if parts[0] == "zero_cancel":
                requests.pop(parts[1], None)
                notice = "已取消，账本未更改"
            else:
                result, duplicate = await self.management.resolve_admin_zero(
                    user.id, user.id, request
                )
                notice = {
                    "applied": "管理员归零已完成",
                    "noop": "余额已为零，未新增清账流水",
                    "expired": "确认已过期，请核实余额后重新操作",
                    "stale": "余额或流水已变化，请核实后重新归零",
                    "failed": "归零未完成，请刷新并核查账本后重试",
                }.get(result["result"], "确认已失效，请重新操作")
                if duplicate:
                    notice = "该确认已处理。" + notice
        else:
            parts = data.split(":")
            try:
                if parts[0] == "state" and len(parts) == 5 and (parts[2] in {"0", "1"}):
                    customer, disabled, kind, page = (
                        int(parts[1]),
                        parts[2] == "1",
                        parts[3],
                        int(parts[4]),
                    )
                elif parts[0] == "zero" and len(parts) == 4:
                    customer, kind, page = (int(parts[1]), parts[2], int(parts[3]))
                else:
                    raise ValueError("invalid management callback")
                if (
                    kind not in {"a", "p", "d", "s", "i"}
                    or customer <= 0
                    or (not 0 <= page <= 999999)
                ):
                    raise ValueError("invalid management context")
            except ValueError:
                await query.answer("按钮已失效，请刷新客户账本", show_alert=True)
                return True
            detail = await self.store.get_customer_detail(user.id, customer)
            if detail is None:
                await query.answer("无权操作此客户账本", show_alert=True)
                return True
            await query.answer()
            if parts[0] == "state":
                await self.management.set_customer_disabled(
                    user.id, user.id, customer, disabled
                )
                notice = "已标记停用，仅为内部账本状态" if disabled else "已恢复正常"
            else:
                request = await self.management.create_admin_zero(
                    user.id, user.id, customer, kind, page
                )
                now = datetime.now(timezone.utc)
                for nonce, old in list(requests.items()):
                    if now >= datetime.fromisoformat(old["expires_at_utc"]):
                        requests.pop(nonce, None)
                while len(requests) >= 16:
                    requests.pop(next(iter(requests)))
                requests[request["operation_id"]] = request
                text, keyboard = build_admin_zero_confirmation(detail, request)
                await safe_edit_query(query, text, keyboard)
                return True
        detail = await self.store.get_customer_detail(user.id, customer)
        if detail is None:
            text, keyboard = build_admin_notice_view(
                "客户账本",
                "账本已不可用，请返回首页核查",
                InlineKeyboardMarkup([admin_navigation()]),
            )
        else:
            text, keyboard = build_customer_detail_view(
                detail, kind, page, notice=notice
            )
        await safe_edit_query(query, text, keyboard)
        return True

    async def on_admin_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        user = update.effective_user
        if query is None or user is None:
            return
        data = query.data or ""

        if (
            query.message is None
            or query.inline_message_id
            or query.message.chat_id != user.id
        ):
            await query.answer("请在机器人私聊中操作自己的账本", show_alert=True)
            return
        if data == "noop":
            await query.answer()
            return
        if not await self.store.is_business_owner(user.id):
            await query.answer("无权查看账本", show_alert=True)
            return
        try:
            if await self.broadcast.on_callback(query, user, context, data):
                return
            context.user_data.pop("awaiting_broadcast_text", None)
            context.user_data.pop("broadcast_preview", None)
            if await self.receipts.on_callback(query, user, context, data):
                return
            if await self.on_customer_management_callback(query, user, context, data):
                return
            if data == "search":
                await query.answer()
                context.user_data["awaiting_customer_search"] = True
                context.user_data.pop("customer_search_query", None)
                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("取消", callback_data="search_cancel"),
                            InlineKeyboardButton("🏠 首页", callback_data="home"),
                        ]
                    ]
                )
                text, keyboard = build_admin_notice_view(
                    "搜索客户", "请输入姓名、@用户名或 Telegram ID", keyboard
                )
                await safe_edit_query(query, text, keyboard)
                return
            if data == "search_cancel":
                await query.answer()
                context.user_data.pop("awaiting_customer_search", None)
                context.user_data.pop("customer_search_query", None)
                text, keyboard = await self.get_admin_home_view(user.id)
                await safe_edit_query(query, text, keyboard)
                return
            if data.startswith("search_page:"):
                await query.answer()
                context.user_data.pop("awaiting_customer_search", None)
                search_query = context.user_data.get("customer_search_query")
                if not search_query:
                    keyboard = InlineKeyboardMarkup(
                        [
                            [admin_button("🔍 重新搜索", "search", "primary")],
                            admin_navigation(),
                        ]
                    )
                    await safe_edit_query(query, "搜索已过期，请重新搜索。", keyboard)
                    return
                try:
                    page = max(0, int(data.split(":", 1)[1]))
                except ValueError:
                    page = 0
                text, keyboard = await self.get_admin_search_view(
                    user.id, search_query, page
                )
                await safe_edit_query(query, text, keyboard)
                return
            if data == "home":
                await query.answer()
                context.user_data.pop("awaiting_customer_search", None)
                text, keyboard = await self.get_admin_home_view(user.id)
                await safe_edit_query(query, text, keyboard)
                return
            if data.startswith("outbox:"):
                await query.answer()
                try:
                    page = max(0, int(data.split(":", 1)[1]))
                except ValueError:
                    page = 0
                entries, total, page = await self.store.get_outbox_attention_page(
                    user.id, page
                )
                text, keyboard = build_outbox_attention_view(entries, total, page)
                await safe_edit_query(query, text, keyboard)
                return
            if data.startswith("history:"):
                await query.answer()
                parts = data.split(":")
                try:
                    if len(parts) != 5:
                        raise ValueError("invalid history callback")
                    customer_chat_id = int(parts[1])
                    page = max(0, int(parts[2]))
                    back_kind = (
                        parts[3] if parts[3] in {"p", "d", "a", "s", "i"} else "a"
                    )
                    back_page = max(0, min(int(parts[4]), 999999))
                except ValueError:
                    await safe_edit_query(
                        query,
                        "按钮已过期，请返回首页重新打开。",
                        InlineKeyboardMarkup([admin_navigation()]),
                    )
                    return
                detail = await self.store.get_customer_detail(user.id, customer_chat_id)
                if detail is None:
                    await safe_edit_query(
                        query,
                        "客户账本不存在，请返回首页刷新。",
                        InlineKeyboardMarkup([admin_navigation()]),
                    )
                    return
                entries, total, page = await self.store.get_ledger_page(
                    user.id, customer_chat_id, page
                )
                text, keyboard = build_customer_history_view(
                    detail, entries, total, page, back_kind, back_page
                )
                await safe_edit_query(query, text, keyboard)
                return
            if data.startswith("list:"):
                await query.answer()
                parts = data.split(":")
                if len(parts) != 3:
                    await safe_edit_query(
                        query,
                        "按钮已过期，请返回首页重新打开。",
                        InlineKeyboardMarkup([admin_navigation()]),
                    )
                    return
                kind = parts[1]
                try:
                    page = int(parts[2])
                except ValueError:
                    page = 0
                if kind not in {"p", "d", "a", "i"}:
                    kind = "a"
                wallet_rows = (
                    await self.store.get_deposit_wallet_rows(user.id)
                    if kind == "i"
                    else await self.store.get_wallet_rows(user.id)
                )
                text, keyboard = build_customer_list_view(wallet_rows, kind, page)
                await safe_edit_query(query, text, keyboard)
                return
            if data.startswith("locate:"):
                parts = data.split(":")
                if len(parts) != 4:
                    await query.answer("按钮数据异常，请刷新", show_alert=True)
                    return
                try:
                    customer_chat_id = int(parts[1])
                except ValueError:
                    await query.answer("客户数据异常，请刷新", show_alert=True)
                    return
                back_kind = parts[2] if parts[2] in {"p", "d", "a", "s", "i"} else "a"
                try:
                    back_page = int(parts[3])
                except ValueError:
                    back_page = 0
                detail = await self.store.get_customer_detail(user.id, customer_chat_id)
                if detail is None:
                    await query.answer("客户账本不存在，请刷新列表", show_alert=True)
                    return
                result = await self.connections.locate_customer_chat(
                    context, owner_user_id=user.id, customer_chat_id=customer_chat_id
                )
                if result.success:
                    await query.answer("已定位，请返回聊天列表查看", show_alert=True)
                    return
                alert_text = locate_failure_message(result.failure_category)
                username = (detail["wallet"]["username"] or "").strip()
                clean_username = username.lstrip("@")
                if re.fullmatch("[A-Za-z0-9_]{5,32}", clean_username):
                    alert_text += "\n无法自动定位，可使用「打开聊天」直接进入会话"
                await query.answer(alert_text, show_alert=True)
                return
            if data.startswith("user:"):
                await query.answer()
                parts = data.split(":")
                if len(parts) != 4:
                    await safe_edit_query(
                        query,
                        "按钮已过期，请返回首页重新打开。",
                        InlineKeyboardMarkup([admin_navigation()]),
                    )
                    return
                try:
                    customer_chat_id = int(parts[1])
                    page = max(0, min(int(parts[3]), 999999))
                except ValueError:
                    await safe_edit_query(
                        query,
                        "客户数据异常，请返回首页刷新。",
                        InlineKeyboardMarkup([admin_navigation()]),
                    )
                    return
                back_kind = parts[2] if parts[2] in {"p", "d", "a", "s", "i"} else "a"
                started = time.perf_counter()
                logger.info(
                    "Admin customer click | owner=%s customer=%s kind=%s page=%s",
                    user.id,
                    customer_chat_id,
                    back_kind,
                    page,
                )
                detail = await self.store.get_customer_detail(user.id, customer_chat_id)
                logger.info(
                    "Admin customer detail loaded | owner=%s customer=%s elapsed=%.3fs",
                    user.id,
                    customer_chat_id,
                    time.perf_counter() - started,
                )
                if detail is None:
                    if back_kind == "s" and context.user_data.get(
                        "customer_search_query"
                    ):
                        search_query = context.user_data["customer_search_query"]
                        text, keyboard = await self.get_admin_search_view(
                            user.id, search_query, page
                        )
                    else:
                        wallet_rows = await self.store.get_wallet_rows(user.id)
                        text, keyboard = build_customer_list_view(
                            wallet_rows, back_kind, page
                        )
                    await safe_edit_query(query, text, keyboard)
                    return
                text, keyboard = build_customer_detail_view(
                    detail, back_kind, page, include_chat_button=True
                )
                try:
                    await safe_edit_query(query, text, keyboard)
                except BadRequest as exc:
                    if not any(
                        (
                            word in str(exc).lower()
                            for word in (
                                "button_url",
                                "button url",
                                "url invalid",
                                "wrong http url",
                            )
                        )
                    ):
                        raise
                    logger.warning(
                        "Customer detail edit with chat button failed; retry without URL | owner=%s customer=%s error=%s",
                        user.id,
                        customer_chat_id,
                        exc,
                    )
                    text, keyboard = build_customer_detail_view(
                        detail, back_kind, page, include_chat_button=False
                    )
                    await safe_edit_query(query, text, keyboard)
                logger.info(
                    "Admin customer detail shown | owner=%s customer=%s total_elapsed=%.3fs",
                    user.id,
                    customer_chat_id,
                    time.perf_counter() - started,
                )
                return
            await query.answer("未知操作，请返回首页", show_alert=True)
        except TelegramError as exc:
            logger.exception(
                "Admin callback Telegram error | owner=%s data=%r error=%s",
                user.id,
                data,
                exc,
            )
            try:
                category = classify_rich_edit_error(exc)
                message = {
                    "uneditable": "此管理消息无法编辑，请发送 /start 重新打开。",
                    "rich_incompatible": "Telegram 当前不支持此富文本页面，请更新客户端后重试。",
                    "temporary_network": "Telegram 暂时响应失败，请稍后再试。",
                }.get(category, "打开失败，请重试；仍失败请发送 /start 重新打开。")
                await query.answer(message, show_alert=True)
            except TelegramError:
                pass
        except Exception as exc:
            logger.exception(
                "Admin callback error | owner=%s data=%r error=%s", user.id, data, exc
            )
            try:
                await query.answer(
                    "处理失败，请重试；仍失败请点首页刷新", show_alert=True
                )
            except TelegramError:
                pass
