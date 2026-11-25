"""Telegram Business connection and private-chat bookkeeping handlers."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from jzbot.money import format_local_time, format_usdt, parse_business_command
from jzbot.outbox import OutboxWorker
from jzbot.store import Store

logger = logging.getLogger(__name__)


def format_query_reply(store: Store, owner_id: int, chat_id: int, tz_offset_hours: int = 8) -> str:
    customer = store.get_customer(owner_id, chat_id)
    if not customer or not customer.has_wallet:
        return "当前余额: 0 USDT\n暂无记账记录"

    txs, _ = store.get_recent_transactions(owner_id, chat_id, limit=5, offset=0)
    lines = [f"当前余额: {format_usdt(customer.balance_tenths)}"]
    if not txs:
        lines.append("暂无记账记录")
        return "\n".join(lines)

    lines.append("最近 5 条流水:")
    for tx in txs:
        ts = format_local_time(tx.created_at, tz_offset_hours)
        if tx.kind == "deposit":
            action = f"+{format_usdt(tx.amount_tenths)}"
        elif tx.kind == "withdraw":
            action = f"-{format_usdt(tx.amount_tenths)}"
        elif tx.kind == "clear":
            action = "账本已清空"
        else:
            action = "管理员归零"
        lines.append(f"{ts} | {action} | 余额 {format_usdt(tx.balance_after)}")
    return "\n".join(lines)


class BusinessHandlers:
    """Handles Telegram Business connections and owner bookkeeping commands."""

    def __init__(self, store: Store, outbox: OutboxWorker, tz_offset_hours: int = 8) -> None:
        self.store = store
        self.outbox = outbox
        self.tz_offset_hours = tz_offset_hours

    async def on_business_connection(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        conn = update.business_connection
        if not conn or not conn.user:
            return
        self.store.upsert_connection(conn.id, conn.user.id, bool(conn.is_enabled))
        logger.info(
            "Updated business connection %s for owner %s (enabled=%s)",
            conn.id,
            conn.user.id,
            conn.is_enabled,
        )
        if conn.is_enabled and self.store.mark_owner_welcomed(conn.user.id):
            try:
                await context.bot.send_message(
                    chat_id=conn.user.id,
                    text=(
                        "✅ 记账机器人已连接 Business 账号。\n"
                        "在客户私聊中发送：\n"
                        "• +10 / -0.5：记一笔入账/出账\n"
                        "• /：查看最近 5 条流水与余额\n"
                        "• //：清零客户余额\n"
                        "私聊本机器人发送 /start 可打开管理后台。"
                    ),
                )
            except TelegramError as exc:
                logger.info("Welcome message to owner %s skipped: %s", conn.user.id, exc)

    async def on_business_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.business_message
        if not msg or not msg.chat or msg.chat.type != ChatType.PRIVATE:
            return
        conn_id = msg.business_connection_id
        if not conn_id:
            return

        conn_info = self.store.get_connection(conn_id)
        if conn_info is None:
            try:
                remote = await context.bot.get_business_connection(conn_id)
                self.store.upsert_connection(remote.id, remote.user.id, bool(remote.is_enabled))
                conn_info = (remote.user.id, bool(remote.is_enabled))
            except TelegramError as exc:
                logger.warning("Failed to fetch unknown business connection %s: %s", conn_id, exc)
                return

        owner_id, is_enabled = conn_info
        if not is_enabled:
            return

        chat = msg.chat
        # Refresh customer profile metadata (does not create a wallet).
        if chat.id != owner_id:
            self.store.upsert_customer_profile(
                owner_id=owner_id,
                chat_id=chat.id,
                username=chat.username,
                first_name=chat.first_name,
                last_name=chat.last_name,
            )

        sender = msg.from_user
        if not sender or sender.is_bot or sender.id != owner_id:
            return

        cmd = parse_business_command(msg.text)
        if cmd is None:
            return

        if cmd.kind == "query":
            reply_text = format_query_reply(self.store, owner_id, chat.id, self.tz_offset_hours)
            try:
                await context.bot.send_message(
                    chat_id=chat.id,
                    text=reply_text,
                    business_connection_id=conn_id,
                    reply_to_message_id=msg.message_id,
                    allow_sending_without_reply=True,
                )
            except TelegramError as exc:
                logger.warning("Query reply failed for chat %s: %s", chat.id, exc)
            return

        if cmd.kind in ("deposit", "withdraw"):
            try:
                res = self.store.record_amount(
                    owner_id=owner_id,
                    chat_id=chat.id,
                    source_message_id=msg.message_id,
                    connection_id=conn_id,
                    kind=cmd.kind,
                    amount_tenths=cmd.amount_tenths,
                    username=chat.username,
                    first_name=chat.first_name,
                    last_name=chat.last_name,
                )
            except OverflowError:
                logger.warning("Ignored overflow amount command from owner %s for chat %s", owner_id, chat.id)
                return
            if res.applied:
                self.outbox.notify()
            return

        if cmd.kind == "clear":
            res = self.store.record_clear(
                owner_id=owner_id,
                chat_id=chat.id,
                source_message_id=msg.message_id,
                connection_id=conn_id,
                username=chat.username,
                first_name=chat.first_name,
                last_name=chat.last_name,
            )
            if res.applied:
                self.outbox.notify()
