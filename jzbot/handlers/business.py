from __future__ import annotations

import logging

from telegram import (
    Update,
)
from telegram.constants import ChatType
from telegram.error import (
    TelegramError,
)
from telegram.ext import ContextTypes

from jzbot.domain import (
    WELCOME_TEXT,
    beijing_display_from_utc,
    format_tenths,
    parse_amount_to_tenths,
)
from jzbot.infra.business import send_business_text

logger = logging.getLogger("jzbot")


class BusinessHandlers:
    def __init__(self, store, connections, outbox, accounting, management):
        self.store = store
        self.connections = connections
        self.outbox = outbox
        self.accounting = accounting
        self.management = management

    async def on_business_connection(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        connection = update.business_connection
        if connection is None:
            return
        try:
            welcome_needed = await self.store.upsert_business_connection(
                connection.id, connection.user.id, connection.is_enabled
            )
        except Exception:
            logger.exception("Failed to store business connection")
            return
        logger.info(
            "Business connection update | id=%s owner=%s enabled=%s",
            connection.id,
            connection.user.id,
            connection.is_enabled,
        )
        if connection.is_enabled and welcome_needed:
            try:
                await context.bot.send_message(
                    chat_id=connection.user_chat_id, text=WELCOME_TEXT
                )
            except TelegramError:
                logger.exception(
                    "Welcome message failed | owner=%s user_chat_id=%s",
                    connection.user.id,
                    connection.user_chat_id,
                )
            else:
                await self.store.mark_welcome_sent(connection.user.id)

    async def on_business_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.business_message
        if message is None:
            return
        if message.chat.type != ChatType.PRIVATE or message.text is None:
            return
        business_connection_id = message.business_connection_id
        if not business_connection_id:
            return
        if message.sender_business_bot is not None:
            return
        connection_info = await self.connections.ensure_connection_info(
            context, business_connection_id
        )
        if connection_info is None:
            return
        owner_user_id, is_enabled = connection_info
        if not is_enabled:
            return
        if message.from_user is None or message.from_user.id != owner_user_id:
            return
        text = message.text.strip()
        customer_chat_id = message.chat.id
        source_message_id = message.message_id
        is_recent_query = text == "/"
        is_clear = text == "//"
        delta_tenths = (
            None if is_recent_query or is_clear else parse_amount_to_tenths(text)
        )
        if not is_recent_query and (not is_clear) and (delta_tenths is None):
            return
        try:
            await self.store.upsert_customer_profile(
                owner_user_id=owner_user_id,
                customer_chat_id=customer_chat_id,
                username=message.chat.username,
                first_name=message.chat.first_name,
                last_name=message.chat.last_name,
            )
        except Exception:
            logger.exception(
                "Customer profile update failed | owner=%s chat=%s",
                owner_user_id,
                customer_chat_id,
            )
        try:
            if is_recent_query:
                rows = await self.store.get_recent_entries(
                    owner_user_id, customer_chat_id, limit=5, oldest_first=True
                )
                if not rows:
                    await send_business_text(
                        context, business_connection_id, customer_chat_id, "没有记录"
                    )
                    return
                lines = [f"最近 {len(rows)} 条记录", ""]
                for row in rows:
                    time_text = beijing_display_from_utc(row["created_at_utc"])
                    entry_type = row["entry_type"]
                    amount = int(row["amount_tenths"])
                    before = int(row["balance_before"])
                    after = int(row["balance_after"])
                    if entry_type == "deposit":
                        line = f"{time_text}  +{format_tenths(amount)} → {format_tenths(after)}"
                    elif entry_type == "withdraw":
                        line = f"{time_text}  -{format_tenths(amount)} → {format_tenths(after)}"
                    else:
                        line = f"{time_text}  清账 {format_tenths(before)} → 0"
                    lines.append(line)
                balance = await self.store.get_balance(owner_user_id, customer_chat_id)
                lines.extend(["", f"当前余额 {format_tenths(balance)} USDT"])
                await send_business_text(
                    context, business_connection_id, customer_chat_id, "\n".join(lines)
                )
                return
            await self.commands.submit(
                context, owner_user_id, customer_chat_id, source_message_id,
                business_connection_id, delta=delta_tenths, clear=is_clear,
            )
        except TelegramError:
            logger.exception(
                "Telegram error | owner=%s chat=%s message=%s",
                owner_user_id,
                customer_chat_id,
                source_message_id,
            )
        except Exception:
            logger.exception(
                "Unhandled error | owner=%s chat=%s message=%s",
                owner_user_id,
                customer_chat_id,
                source_message_id,
            )

