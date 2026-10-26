from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, NetworkError, TimedOut

import compat_api as jzbot


def business_connection(
    connection_id: str,
    owner_id: int,
    *,
    is_enabled: bool = True,
    can_reply: bool | None = True,
):
    rights = (
        None
        if can_reply is None
        else SimpleNamespace(can_reply=can_reply)
    )
    return SimpleNamespace(
        id=connection_id,
        user=SimpleNamespace(id=owner_id),
        is_enabled=is_enabled,
        rights=rights,
    )


def locate_context(connection_or_error, send_result=None):
    get_business_connection = AsyncMock()
    if isinstance(connection_or_error, list):
        get_business_connection.side_effect = connection_or_error
    elif isinstance(connection_or_error, Exception):
        get_business_connection.side_effect = connection_or_error
    else:
        get_business_connection.return_value = connection_or_error

    send_message = AsyncMock(
        return_value=send_result or SimpleNamespace(message_id=123)
    )
    return SimpleNamespace(
        user_data={},
        bot=SimpleNamespace(
            get_business_connection=get_business_connection,
            send_message=send_message,
        ),
    )


async def add_customer(
    database,
    owner_id: int,
    customer_id: int,
    *,
    username: str | None = None,
):
    await database.upsert_customer_profile(
        owner_id,
        customer_id,
        username,
        "Test",
        "Customer",
    )
    await database.apply_amount(
        owner_id,
        customer_id,
        100,
        10,
        "business-1",
    )


async def financial_snapshot(database):
    result = {}
    for table in (
        "wallets",
        "ledger",
        "wallet_stats",
        "processed_messages",
        "receipt_outbox",
    ):
        rows = await (
            await database.conn.execute(
                f"SELECT * FROM {table} ORDER BY rowid"
            )
        ).fetchall()
        result[table] = [tuple(row) for row in rows]
    return result


@pytest.mark.asyncio
async def test_locate_refreshes_connection_and_succeeds(
    ledger_db,
    monkeypatch,
    owner_id,
    customer_id,
):
    monkeypatch.setattr(jzbot, "db", ledger_db)
    context = locate_context(business_connection("business-1", owner_id))

    result = await jzbot.locate_customer_chat(
        context,
        owner_id,
        customer_id,
    )

    assert result == jzbot.LocateResult(True, "business-1")
    context.bot.get_business_connection.assert_awaited_once_with("business-1")
    context.bot.send_message.assert_awaited_once_with(
        chat_id=customer_id,
        text=".",
        business_connection_id="business-1",
        disable_notification=True,
    )


@pytest.mark.asyncio
async def test_locate_tries_second_connection_after_first_is_invalid(
    ledger_db,
    monkeypatch,
    owner_id,
    customer_id,
):
    await ledger_db.upsert_business_connection("business-2", owner_id, True)
    monkeypatch.setattr(jzbot, "db", ledger_db)
    monkeypatch.setattr(
        ledger_db,
        "get_enabled_connection_ids",
        AsyncMock(return_value=["business-1", "business-2"]),
    )
    context = locate_context(
        [
            BadRequest("BUSINESS_CONNECTION_INVALID"),
            business_connection("business-2", owner_id),
        ]
    )

    result = await jzbot.locate_customer_chat(
        context,
        owner_id,
        customer_id,
    )

    assert result == jzbot.LocateResult(True, "business-2")
    assert context.bot.get_business_connection.await_count == 2
    assert context.bot.send_message.await_args.kwargs[
        "business_connection_id"
    ] == "business-2"
    assert await ledger_db.get_connection_owner("business-1") == (
        owner_id,
        False,
    )


@pytest.mark.asyncio
async def test_disabled_refreshed_connection_is_not_used(
    ledger_db,
    monkeypatch,
    owner_id,
    customer_id,
):
    monkeypatch.setattr(jzbot, "db", ledger_db)
    context = locate_context(
        business_connection("business-1", owner_id, is_enabled=False)
    )

    result = await jzbot.locate_customer_chat(
        context,
        owner_id,
        customer_id,
    )

    assert result.failure_category == jzbot.LocateFailureCategory.CONNECTION_DISABLED
    context.bot.send_message.assert_not_awaited()
    assert await ledger_db.get_connection_owner("business-1") == (
        owner_id,
        False,
    )


@pytest.mark.asyncio
async def test_connection_without_reply_permission_is_not_used(
    ledger_db,
    monkeypatch,
    owner_id,
    customer_id,
):
    monkeypatch.setattr(jzbot, "db", ledger_db)
    context = locate_context(
        business_connection("business-1", owner_id, can_reply=False)
    )

    result = await jzbot.locate_customer_chat(
        context,
        owner_id,
        customer_id,
    )

    assert result.failure_category == jzbot.LocateFailureCategory.NO_REPLY_PERMISSION
    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_business_connection_invalid_is_classified_and_disabled(
    ledger_db,
    monkeypatch,
    owner_id,
    customer_id,
):
    monkeypatch.setattr(jzbot, "db", ledger_db)
    context = locate_context(BadRequest("BUSINESS_CONNECTION_INVALID"))

    result = await jzbot.locate_customer_chat(
        context,
        owner_id,
        customer_id,
    )

    assert result.failure_category == jzbot.LocateFailureCategory.CONNECTION_INVALID
    context.bot.send_message.assert_not_awaited()
    assert await ledger_db.get_connection_owner("business-1") == (
        owner_id,
        False,
    )


@pytest.mark.asyncio
async def test_customer_unreachable_has_specific_result(
    ledger_db,
    monkeypatch,
    owner_id,
    customer_id,
):
    monkeypatch.setattr(jzbot, "db", ledger_db)
    context = locate_context(business_connection("business-1", owner_id))
    context.bot.send_message.side_effect = BadRequest("PEER_ID_INVALID")

    result = await jzbot.locate_customer_chat(
        context,
        owner_id,
        customer_id,
    )

    assert result.failure_category == jzbot.LocateFailureCategory.CUSTOMER_UNREACHABLE
    assert "可能已超出可回复条件" in jzbot.locate_failure_message(
        result.failure_category
    )


@pytest.mark.asyncio
async def test_timeout_is_temporary_and_does_not_disable_connection(
    ledger_db,
    monkeypatch,
    owner_id,
    customer_id,
):
    monkeypatch.setattr(jzbot, "db", ledger_db)
    context = locate_context(TimedOut())

    result = await jzbot.locate_customer_chat(
        context,
        owner_id,
        customer_id,
    )

    assert result.failure_category == jzbot.LocateFailureCategory.TEMPORARY
    assert await ledger_db.get_connection_owner("business-1") == (
        owner_id,
        True,
    )


@pytest.mark.asyncio
async def test_unknown_telegram_error_uses_safe_fallback(
    ledger_db,
    monkeypatch,
    owner_id,
    customer_id,
    caplog,
):
    monkeypatch.setattr(jzbot, "db", ledger_db)
    context = locate_context(BadRequest("strange telegram response"))

    result = await jzbot.locate_customer_chat(
        context,
        owner_id,
        customer_id,
    )

    assert result.failure_category == jzbot.LocateFailureCategory.UNKNOWN
    assert jzbot.locate_failure_message(result.failure_category) == (
        "定位失败，请稍后重试"
    )
    assert "exception_type=BadRequest" in caplog.text
    assert "business_connection=business-1" in caplog.text


@pytest.mark.asyncio
async def test_network_error_does_not_disable_connection(
    ledger_db,
    monkeypatch,
    owner_id,
    customer_id,
):
    monkeypatch.setattr(jzbot, "db", ledger_db)
    context = locate_context(NetworkError("connection reset by peer"))

    result = await jzbot.locate_customer_chat(
        context,
        owner_id,
        customer_id,
    )

    assert result.failure_category == jzbot.LocateFailureCategory.TEMPORARY
    assert await ledger_db.get_connection_owner("business-1") == (
        owner_id,
        True,
    )


@pytest.mark.asyncio
async def test_username_button_remains_and_failed_callback_mentions_fallback(
    ledger_db,
    monkeypatch,
    owner_id,
    customer_id,
):
    await add_customer(
        ledger_db,
        owner_id,
        customer_id,
        username="public_user",
    )
    monkeypatch.setattr(jzbot, "db", ledger_db)
    context = locate_context(business_connection("business-1", owner_id))
    context.bot.send_message.side_effect = BadRequest("PEER_ID_INVALID")
    query = SimpleNamespace(
        data=f"locate:{customer_id}:a:0",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=SimpleNamespace(chat_id=owner_id),
        inline_message_id=None,
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=owner_id),
    )

    detail = await ledger_db.get_customer_detail(owner_id, customer_id)
    _, keyboard = jzbot.build_customer_detail_view(detail, "a", 0)
    assert any(
        button.text == "💬 打开聊天"
        for row in keyboard.inline_keyboard
        for button in row
    )

    await jzbot.on_admin_callback(update, context)

    alert = query.answer.await_args.args[0]
    assert "该客户当前无法通过 Business Bot 主动联系" in alert
    assert "可使用「打开聊天」直接进入会话" in alert
    assert query.answer.await_args.kwargs == {"show_alert": True}


@pytest.mark.asyncio
async def test_customer_without_username_still_has_locate_button(
    ledger_db,
    owner_id,
    customer_id,
):
    await add_customer(ledger_db, owner_id, customer_id)
    detail = await ledger_db.get_customer_detail(owner_id, customer_id)

    _, keyboard = jzbot.build_customer_detail_view(detail, "a", 0)

    buttons = [
        button
        for row in keyboard.inline_keyboard
        for button in row
    ]
    assert any(button.text == "📍 定位聊天" for button in buttons)
    assert not any(button.text == "💬 打开聊天" for button in buttons)


@pytest.mark.asyncio
async def test_owner_cannot_use_another_owners_connection(
    ledger_db,
    monkeypatch,
    owner_id,
    customer_id,
):
    other_owner = owner_id + 1
    await ledger_db.mark_business_connection_disabled("business-1", owner_id)
    await ledger_db.upsert_business_connection(
        "business-other",
        other_owner,
        True,
    )
    monkeypatch.setattr(jzbot, "db", ledger_db)
    context = locate_context(
        business_connection("business-other", other_owner)
    )

    result = await jzbot.locate_customer_chat(
        context,
        owner_id,
        customer_id,
    )

    assert result.failure_category == jzbot.LocateFailureCategory.NO_CONNECTION
    context.bot.get_business_connection.assert_not_awaited()
    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_forged_customer_callback_is_rejected_before_locate(
    ledger_db,
    monkeypatch,
    owner_id,
):
    monkeypatch.setattr(jzbot, "db", ledger_db)
    query = SimpleNamespace(
        data="locate:999999:a:0",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=SimpleNamespace(chat_id=owner_id),
        inline_message_id=None,
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=owner_id),
    )
    context = locate_context(business_connection("business-1", owner_id))

    await jzbot.on_admin_callback(update, context)

    query.answer.assert_awaited_once_with(
        "客户账本不存在，请刷新列表",
        show_alert=True,
    )
    context.bot.get_business_connection.assert_not_awaited()
    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_callback_answers_only_with_final_alert(
    ledger_db,
    monkeypatch,
    owner_id,
    customer_id,
):
    await add_customer(ledger_db, owner_id, customer_id)
    monkeypatch.setattr(jzbot, "db", ledger_db)
    context = locate_context(business_connection("business-1", owner_id))
    query = SimpleNamespace(
        data=f"locate:{customer_id}:a:0",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=SimpleNamespace(chat_id=owner_id),
        inline_message_id=None,
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=owner_id),
    )

    await jzbot.on_admin_callback(update, context)

    query.answer.assert_awaited_once_with(
        "已定位，请返回聊天列表查看",
        show_alert=True,
    )
    context.bot.send_message.assert_awaited_once_with(
        chat_id=customer_id,
        text=".",
        business_connection_id="business-1",
        disable_notification=True,
    )


@pytest.mark.asyncio
async def test_locate_failure_does_not_change_accounting_tables(
    ledger_db,
    monkeypatch,
    owner_id,
    customer_id,
):
    await add_customer(ledger_db, owner_id, customer_id)
    before = await financial_snapshot(ledger_db)
    monkeypatch.setattr(jzbot, "db", ledger_db)
    context = locate_context(business_connection("business-1", owner_id))
    context.bot.send_message.side_effect = BadRequest("PEER_ID_INVALID")

    result = await jzbot.locate_customer_chat(
        context,
        owner_id,
        customer_id,
    )

    after = await financial_snapshot(ledger_db)
    assert result.failure_category == jzbot.LocateFailureCategory.CUSTOMER_UNREACHABLE
    assert after == before


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (
            BadRequest("BUSINESS_PEER_USAGE_MISSING"),
            jzbot.LocateFailureCategory.CUSTOMER_UNREACHABLE,
        ),
        (
            BadRequest("bot was blocked by the user"),
            jzbot.LocateFailureCategory.CUSTOMER_BLOCKED,
        ),
        (
            BadRequest("not enough rights to send on behalf"),
            jzbot.LocateFailureCategory.NO_REPLY_PERMISSION,
        ),
    ],
)
def test_additional_error_categories(error, category):
    assert jzbot.classify_locate_error(error) == category
