from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import compat_api as jzbot


async def add_customer(
    database,
    owner,
    customer,
    source,
    *,
    username,
    first_name,
    last_name,
    connection="business-1",
):
    await database.upsert_customer_profile(
        owner,
        customer,
        username,
        first_name,
        last_name,
    )
    await database.apply_amount(
        owner,
        customer,
        source,
        10,
        connection,
    )


@pytest.mark.asyncio
async def test_search_supported_fields_and_owner_isolation(
    ledger_db, owner_id
):
    await add_customer(
        ledger_db,
        owner_id,
        555001,
        1,
        username="Abc123",
        first_name="Zhang",
        last_name="San",
    )
    other_owner = 1002
    await ledger_db.upsert_business_connection("business-2", other_owner, True)
    await add_customer(
        ledger_db,
        other_owner,
        555002,
        2,
        username="abc123_hidden",
        first_name="Zhang",
        last_name="San",
        connection="business-2",
    )

    for query in (
        "abc123",
        "@ABC123",
        "zha",
        "san",
        "zhang san",
        "555001",
        "5001",
    ):
        rows, total = await ledger_db.search_customers(owner_id, query)
        assert total == 1, query
        assert [row["customer_chat_id"] for row in rows] == [555001]

    rows, total = await ledger_db.search_customers(owner_id, "hidden")
    assert rows == []
    assert total == 0


@pytest.mark.asyncio
async def test_search_empty_result_and_pagination(ledger_db, owner_id):
    for index in range(12):
        await add_customer(
            ledger_db,
            owner_id,
            700000 + index,
            100 + index,
            username=f"client{index}",
            first_name="Page",
            last_name=f"Customer {index}",
        )

    first, total = await ledger_db.search_customers(
        owner_id, "page", limit=jzbot.ADMIN_PAGE_SIZE, offset=0
    )
    second, second_total = await ledger_db.search_customers(
        owner_id,
        "page",
        limit=jzbot.ADMIN_PAGE_SIZE,
        offset=jzbot.ADMIN_PAGE_SIZE,
    )
    assert total == second_total == 12
    assert len(first) == 10
    assert len(second) == 2
    assert {row["customer_chat_id"] for row in first}.isdisjoint(
        {row["customer_chat_id"] for row in second}
    )

    text, keyboard = jzbot.build_search_results_view("page", first, total, 0)
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert "找到 12 位客户" in text
    assert "search_page:1" in callbacks

    rows, total = await ledger_db.search_customers(owner_id, "does-not-exist")
    text, keyboard = jzbot.build_search_results_view(
        "does-not-exist", rows, total, 0
    )
    assert "没有匹配的客户" in text


@pytest.mark.asyncio
async def test_click_search_result_uses_existing_detail_view(
    ledger_db, monkeypatch, owner_id
):
    await add_customer(
        ledger_db,
        owner_id,
        888001,
        1,
        username="detailuser",
        first_name="Detail",
        last_name="Person",
    )
    monkeypatch.setattr(jzbot, "db", ledger_db)
    query = SimpleNamespace(
        data="user:888001:s:0",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=owner_id),
    )
    context = SimpleNamespace(
        user_data={"customer_search_query": "detail"},
        bot=SimpleNamespace(_post=AsyncMock(return_value={"message_id": 1})),
    )
    query.inline_message_id = None
    query.message = SimpleNamespace(chat_id=owner_id, message_id=1)
    query.get_bot = lambda: context.bot

    await jzbot.on_admin_callback(update, context)

    query.answer.assert_awaited_once()
    query.edit_message_text.assert_not_awaited()
    context.bot._post.assert_awaited_once()
    rendered = context.bot._post.await_args.kwargs["data"]
    assert "Detail Person" in rendered["rich_message"]["html"]
    assert 'type="callback_data" data="search_page:0"' in rendered["rich_message"]["html"]
    assert rendered["reply_markup"]["inline_keyboard"] == []


@pytest.mark.asyncio
async def test_private_search_state_is_one_shot_and_empty_result(
    ledger_db, monkeypatch, owner_id
):
    monkeypatch.setattr(jzbot, "db", ledger_db)
    reply_text = AsyncMock()
    update = SimpleNamespace(
        message=SimpleNamespace(text="nobody", reply_text=reply_text),
        effective_user=SimpleNamespace(id=owner_id),
        effective_chat=SimpleNamespace(type="private", id=owner_id),
    )
    context = SimpleNamespace(
        user_data={"awaiting_customer_search": True},
        bot=SimpleNamespace(_post=AsyncMock(return_value={"message_id": 1})),
    )

    await jzbot.on_admin_search_text(update, context)
    assert "awaiting_customer_search" not in context.user_data
    assert context.user_data["customer_search_query"] == "nobody"
    assert "没有匹配的客户" in context.bot._post.await_args.kwargs["data"]["rich_message"]["html"]

    reply_text.reset_mock()
    context.bot._post.reset_mock()
    await jzbot.on_admin_search_text(update, context)
    reply_text.assert_not_awaited()
    context.bot._post.assert_not_awaited()
