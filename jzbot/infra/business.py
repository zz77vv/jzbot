from __future__ import annotations

import logging

from telegram import (
    ReplyParameters,
)
from telegram.error import (
    TelegramError,
)
from telegram.ext import ContextTypes

logger = logging.getLogger("jzbot")


async def send_business_text(
    context: ContextTypes.DEFAULT_TYPE,
    business_connection_id: str,
    chat_id: int,
    text: str,
    reply_to_message_id: int | None = None,
    disable_notification: bool = False,
):
    kwargs = {
        "chat_id": chat_id,
        "text": text,
        "business_connection_id": business_connection_id,
        "disable_notification": disable_notification,
    }

    if reply_to_message_id is not None:
        kwargs["reply_parameters"] = ReplyParameters(message_id=reply_to_message_id)

    return await context.bot.send_message(**kwargs)


async def try_pin_business_message(
    context: ContextTypes.DEFAULT_TYPE,
    business_connection_id: str,
    chat_id: int,
    message_id: int,
) -> None:
    try:
        await context.bot.pin_chat_message(
            chat_id=chat_id,
            message_id=message_id,
            business_connection_id=business_connection_id,
            disable_notification=True,
        )
    except TelegramError:
        # 置顶失败绝不能影响已完成的账务
        logger.exception(
            "Pin failed | business_connection=%s chat=%s message=%s",
            business_connection_id,
            chat_id,
            message_id,
        )
