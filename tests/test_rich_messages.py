import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.request import BaseRequest

import compat_api as jzbot
from jzbot.infra.rich import edit_admin_rich, send_admin_rich


class RecordingRequest(BaseRequest):
    """Exercise the actual installed PTB serialization without Telegram access."""
    @property
    def read_timeout(self):
        return 5

    def __init__(self, response=None, status=200):
        self.calls = []
        self.status = status
        self.response = response or {
            "ok": True,
            "result": {
                "message_id": 17,
                "rich_message": {"blocks": [{"type": "table", "is_bordered": True}]},
            },
        }

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        self.calls.append((url.rsplit("/", 1)[1], request_data.json_parameters))
        return self.status, json.dumps(self.response).encode()


@pytest.mark.asyncio
async def test_real_ptb_transport_serializes_send_rich_message_and_styles():
    request = RecordingRequest()
    bot = Bot("123456:TEST_TOKEN", request=request)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("存款", callback_data="list:p:0", style="success")]])
    html = "<table bordered><tr><td>客户</td><td>42</td></tr></table>"
    result = await send_admin_rich(bot, 1001, html, keyboard)
    endpoint, params = request.calls[0]
    assert endpoint == "sendRichMessage"
    rendered = json.loads(params["rich_message"])["html"]
    assert rendered.startswith(html)
    assert 'type="callback_data" data="list:p:0" style="success"' in rendered
    assert json.loads(params["reply_markup"])["inline_keyboard"] == []
    assert "text" not in params and "parse_mode" not in params
    assert result["rich_message"]["blocks"][0]["is_bordered"] is True


@pytest.mark.asyncio
async def test_actual_ptb_callback_parses_rich_message_and_edits_same_id():
    request = RecordingRequest()
    bot = Bot("123456:TEST_TOKEN", request=request)
    update = Update.de_json({
        "update_id": 1,
        "callback_query": {
            "id": "q", "from": {"id": 1001, "is_bot": False, "first_name": "Admin"},
            "chat_instance": "instance", "data": "home",
            "message": {"message_id": 17, "date": 1, "chat": {"id": 1001, "type": "private"},
                        "rich_message": {"blocks": [{"type": "table", "is_bordered": True}]}},
        },
    }, bot)
    html, keyboard = jzbot.build_home_view([])
    await edit_admin_rich(update.callback_query, html, keyboard)
    endpoint, params = request.calls[0]
    assert endpoint == "editMessageText"
    assert params["message_id"] == "17" and params["chat_id"] == "1001"
    assert "<table bordered striped compact>" in json.loads(params["rich_message"])["html"]
    assert "text" not in params and "parse_mode" not in params
    assert "rich_message" in update.callback_query.message.api_kwargs


@pytest.mark.asyncio
async def test_inline_edit_also_passes_only_rich_content():
    bot = SimpleNamespace(_post=AsyncMock(return_value=True))
    query = SimpleNamespace(inline_message_id="inline-id", message=None, get_bot=Mock(return_value=bot))
    _, keyboard = jzbot.build_home_view([])
    await edit_admin_rich(query, "<p>Rich</p>", keyboard)
    data = bot._post.await_args.kwargs["data"]
    assert data["inline_message_id"] == "inline-id"
    assert "chat_id" not in data and "text" not in data


@pytest.mark.asyncio
async def test_api_rejection_is_not_replaced_with_ordinary_message():
    request = RecordingRequest({"ok": False, "error_code": 400, "description": "Bad Request: rich messages are not allowed"}, 400)
    bot = Bot("123456:TEST_TOKEN", request=request)
    with pytest.raises(BadRequest, match="(?i)rich messages are not allowed"):
        await send_admin_rich(bot, 1001, "<p>Rich</p>")
    assert [call[0] for call in request.calls] == ["sendRichMessage"]


@pytest.mark.asyncio
async def test_inaccessible_callback_and_malformed_response_are_errors():
    bot = SimpleNamespace(_post=AsyncMock(return_value=False))
    _, keyboard = jzbot.build_home_view([])
    query = SimpleNamespace(inline_message_id=None, message=None, get_bot=Mock(return_value=bot))
    with pytest.raises(TelegramError, match="inaccessible"):
        await edit_admin_rich(query, "<p>Rich</p>", keyboard)
    bot._post.assert_not_awaited()
    with pytest.raises(TelegramError, match="unexpected"):
        await send_admin_rich(bot, 1001, "<p>Rich</p>")


def test_two_column_table_escapes_user_content_and_has_real_cells():
    table = jzbot.admin_rich_table([("<script>&客户", "<value>")])
    assert table.startswith("<table bordered striped compact>")
    assert table.count("<tr>") == 2 and table.count("<td>") == 2
    assert "&lt;script&gt;&amp;客户" in table and "&lt;value&gt;" in table
    assert "<script>" not in table
