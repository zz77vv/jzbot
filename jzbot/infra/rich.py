"""Official Rich Messages endpoints for the installed PTB 22.8 transport.

PTB 22.8 has no native RichMessage type. Keep the raw API result instead of
decoding it as a PTB Message, which would otherwise lose its rich structure.
Only this module touches PTB's inspected/tested low-level `_post` transport.
There is deliberately no ordinary sendMessage fallback.
"""

import logging
import re
from html import escape

from telegram.error import BadRequest, NetworkError, TelegramError

logger = logging.getLogger("jzbot")


def rich_button_rows(reply_markup) -> str:
    """Render the existing safe button definitions as official Rich HTML."""
    if reply_markup is None:
        return ""
    rows = []
    for row in reply_markup.inline_keyboard:
        if not 1 <= len(row) <= 8:
            raise ValueError("Rich button rows require 1-8 buttons")
        rendered = []
        for button in row:
            data = button.to_dict()
            style = data.get("style")
            attrs = []
            if data.get("callback_data") is not None:
                callback = data["callback_data"]
                if not 1 <= len(callback.encode("utf-8")) <= 64:
                    raise ValueError("Rich callback_data exceeds Telegram limits")
                attrs = [("type", "callback_data"), ("data", callback)]
            elif data.get("url"):
                attrs = [("type", "url"), ("url", data["url"])]
            elif data.get("copy_text"):
                copied = data["copy_text"]["text"]
                if not 1 <= len(copied.encode("utf-16-le")) // 2 <= 256:
                    raise ValueError("Rich copy_text exceeds Telegram limits")
                attrs = [("type", "copy_text"), ("text", copied)]
            else:
                raise ValueError("Unsupported admin Rich button action")
            if style:
                attrs.append(("style", style))
            attributes = " ".join(
                f'{key}="{escape(value, quote=True)}"' for key, value in attrs
            )
            rendered.append(
                f"<tg-button {attributes}>{escape(button.text)}</tg-button>"
            )
        rows.append(
            '<tg-button-row align="left">' + "".join(rendered) + "</tg-button-row>"
        )
    return "".join(rows)


def prepare_admin_page(html: str, reply_markup=None) -> dict:
    # Builders retain the keyboard as a compatibility definition, not a second UI.
    if reply_markup is not None and "<tg-button" not in html:
        html += rich_button_rows(reply_markup)
    return {"rich_message": {"html": html}, "reply_markup": {"inline_keyboard": []}}


def classify_rich_edit_error(exc: TelegramError) -> str:
    message = str(exc).lower()
    if isinstance(exc, NetworkError) and not isinstance(exc, BadRequest):
        return "temporary_network"
    if "message is not modified" in message:
        return "not_modified"
    if (
        "can't be edited" in message
        or "cannot be edited" in message
        or "message to edit not found" in message
    ):
        return "uneditable"
    if "rich" in message and any(
        word in message for word in ("unsupported", "not allowed", "invalid")
    ):
        return "rich_incompatible"
    return "api_error"


async def _page_call(bot, method, data, reply_markup):
    try:
        return await _call(bot, method, data)
    except BadRequest as exc:
        message = str(exc).lower()
        # One bounded retry, ONLY for explicit unsupported Rich body buttons.
        # Tables/details remain Rich Messages; no ordinary sendMessage fallback.
        unsupported_buttons = any(
            word in message for word in ("tg-button", "rich button", "buttons block")
        ) and any(
            word in message for word in ("unsupported", "not supported", "unknown tag")
        )
        if not unsupported_buttons or reply_markup is None:
            raise
        logger.warning(
            "Admin Rich body buttons unsupported; using inline controls | method=%s",
            method,
        )
        fallback = dict(data)
        html = re.sub(
            r"<tg-button-row\b[^>]*>.*?</tg-button-row>",
            "",
            data["rich_message"]["html"],
            flags=re.S,
        )
        fallback["rich_message"] = {
            "html": html
            + "<footer>当前接口不支持正文按钮，已使用底部操作按钮。</footer>"
        }
        fallback["reply_markup"] = reply_markup.to_dict()
        return await _call(bot, method, fallback)


async def _call(bot, method: str, data: dict):
    result = await bot._post(method, data=data)
    if not isinstance(result, dict) and result is not True:
        raise TelegramError(f"{method}: unexpected Bot API response")
    return result


async def send_admin_rich(bot, chat_id: int, html: str, reply_markup=None):
    data = {"chat_id": chat_id, **prepare_admin_page(html, reply_markup)}
    return await _page_call(bot, "sendRichMessage", data, reply_markup)


async def edit_admin_rich(query, html: str, reply_markup):
    data = prepare_admin_page(html, reply_markup)
    if query.inline_message_id:
        data["inline_message_id"] = query.inline_message_id
    elif query.message is not None:
        data["chat_id"] = query.message.chat_id
        data["message_id"] = query.message.message_id
    else:
        raise TelegramError("Rich message is inaccessible; reopen /start")
    try:
        return await _page_call(query.get_bot(), "editMessageText", data, reply_markup)
    except TelegramError as exc:
        # Never resend a page automatically: stale buttons cannot create a loop.
        logger.warning(
            "Admin Rich edit failed | chat=%s message=%s category=%s exception_type=%s",
            data.get("chat_id"),
            data.get("message_id"),
            classify_rich_edit_error(exc),
            type(exc).__name__,
        )
        raise
