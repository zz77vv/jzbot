from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import TelegramError

import compat_api as jzbot


async def create_amount(database, owner, customer, source=1, amount=100):
    return await database.apply_amount(
        owner,
        customer,
        source,
        amount,
        "business-1",
    )


@pytest.mark.asyncio
async def test_accounting_and_outbox_are_one_transaction(
    ledger_db, monkeypatch, owner_id, customer_id
):
    async def fail_outbox(*args, **kwargs):
        raise RuntimeError("outbox insert failed")

    monkeypatch.setattr(ledger_db, "_insert_receipt_outbox", fail_outbox)
    with pytest.raises(RuntimeError, match="outbox insert failed"):
        await create_amount(ledger_db, owner_id, customer_id)

    assert await ledger_db.get_balance(owner_id, customer_id) == 0
    for table in ("ledger", "processed_messages", "receipt_outbox"):
        row = await (
            await ledger_db.conn.execute(f"SELECT COUNT(*) AS count FROM {table}")
        ).fetchone()
        assert row["count"] == 0


@pytest.mark.asyncio
async def test_successful_send_marks_delivered(
    ledger_db, monkeypatch, successful_context, owner_id, customer_id
):
    monkeypatch.setattr(jzbot, "db", ledger_db)
    result = await create_amount(ledger_db, owner_id, customer_id)

    assert await jzbot.deliver_outbox_receipt(
        successful_context, result["outbox_id"]
    )
    row = await ledger_db.get_outbox(result["outbox_id"])
    assert row["status"] == "delivered"
    assert row["delivered_at_utc"] is not None
    successful_context.bot.send_message.assert_awaited_once()
    assert successful_context.bot.send_message.await_args.kwargs["text"] == "+10｜余10U"
    successful_context.bot.pin_chat_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_failure_keeps_accounting_and_pending(
    ledger_db, monkeypatch, owner_id, customer_id
):
    monkeypatch.setattr(jzbot, "db", ledger_db)
    context = SimpleNamespace(
        bot=SimpleNamespace(
            send_message=AsyncMock(side_effect=TelegramError("network down")),
            pin_chat_message=AsyncMock(),
        )
    )
    result = await create_amount(ledger_db, owner_id, customer_id)

    assert not await jzbot.deliver_outbox_receipt(context, result["outbox_id"])
    assert await ledger_db.get_balance(owner_id, customer_id) == 100
    processed = await (
        await ledger_db.conn.execute(
            "SELECT COUNT(*) AS count FROM processed_messages"
        )
    ).fetchone()
    row = await ledger_db.get_outbox(result["outbox_id"])
    assert processed["count"] == 1
    assert row["status"] == "pending"
    assert row["attempt_count"] == 1
    assert row["last_error"] == "network down"


@pytest.mark.asyncio
async def test_restart_loads_pending_receipt_from_sqlite(
    tmp_path, monkeypatch, successful_context, owner_id, customer_id
):
    path = tmp_path / "restart.db"
    first = jzbot.LedgerDB(path)
    await first.open()
    await first.upsert_business_connection("business-1", owner_id, True)
    result = await create_amount(first, owner_id, customer_id)
    await first.close()

    restarted = jzbot.LedgerDB(path)
    await restarted.open()
    monkeypatch.setattr(jzbot, "db", restarted)
    try:
        assert await jzbot.process_due_outbox(successful_context) == 1
        row = await restarted.get_outbox(result["outbox_id"])
        assert row["status"] == "delivered"
        assert await restarted.get_balance(owner_id, customer_id) == 100
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_duplicate_delivery_creates_no_second_account_or_outbox(
    ledger_db, owner_id, customer_id
):
    first = await create_amount(ledger_db, owner_id, customer_id, source=44)
    duplicate = await create_amount(ledger_db, owner_id, customer_id, source=44)

    assert duplicate["duplicate"] is True
    assert duplicate["outbox_id"] == first["outbox_id"]
    counts = {}
    for table in ("ledger", "processed_messages", "receipt_outbox"):
        row = await (
            await ledger_db.conn.execute(f"SELECT COUNT(*) AS count FROM {table}")
        ).fetchone()
        counts[table] = row["count"]
    assert counts == {"ledger": 1, "processed_messages": 1, "receipt_outbox": 1}


@pytest.mark.asyncio
async def test_backoff_without_sleep(ledger_db, owner_id, customer_id):
    result = await create_amount(ledger_db, owner_id, customer_id)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    expected_delays = [10, 30, 120, 300, 300]
    for attempt, delay in enumerate(expected_delays, start=1):
        await ledger_db.mark_outbox_failed(
            result["outbox_id"],
            RuntimeError(f"failure-{attempt}"),
            now=now,
        )
        row = await ledger_db.get_outbox(result["outbox_id"])
        assert row["attempt_count"] == attempt
        retry_at = datetime.fromisoformat(row["next_attempt_at_utc"])
        assert (retry_at - now).total_seconds() == delay


@pytest.mark.asyncio
async def test_clear_also_creates_receipt_when_balance_is_zero(
    ledger_db, owner_id, customer_id
):
    result = await ledger_db.clear_balance(
        owner_id, customer_id, 88, "business-1"
    )
    row = await ledger_db.get_outbox(result["outbox_id"])
    assert result["changed"] is False
    assert row["receipt_type"] == "clear"
    assert row["should_pin"] == 0
    assert row["receipt_text"] == "余额已清零"


@pytest.mark.asyncio
async def test_minimal_receipt_templates_and_recent_entries_unchanged(
    ledger_db, owner_id, customer_id
):
    deposit = await create_amount(
        ledger_db, owner_id, customer_id, source=201, amount=150
    )
    withdraw = await create_amount(
        ledger_db, owner_id, customer_id, source=202, amount=-30
    )
    fraction = await create_amount(
        ledger_db, owner_id, customer_id, source=203, amount=5
    )
    half_withdraw = await create_amount(
        ledger_db, owner_id, customer_id, source=204, amount=-5
    )
    negative = await create_amount(
        ledger_db, owner_id, customer_id, source=205, amount=-200
    )

    assert deposit["receipt_text"] == "+15｜余15U"
    assert withdraw["receipt_text"] == "-3｜余12U"
    assert fraction["receipt_text"] == "+0.5｜余12.5U"
    assert half_withdraw["receipt_text"] == "-0.5｜余12U"
    assert negative["receipt_text"] == "-20｜余-8U"

    stored = []
    for result in (deposit, withdraw, fraction, half_withdraw, negative):
        row = await ledger_db.get_outbox(result["outbox_id"])
        stored.append(row["receipt_text"])
        assert row["should_pin"] == 1
    assert stored == [
        "+15｜余15U",
        "-3｜余12U",
        "+0.5｜余12.5U",
        "-0.5｜余12U",
        "-20｜余-8U",
    ]

    recent = await ledger_db.get_recent_entries(
        owner_id, customer_id, limit=5, oldest_first=True
    )
    assert [row["entry_type"] for row in recent] == [
        "deposit",
        "withdraw",
        "deposit",
        "withdraw",
        "withdraw",
    ]
    assert [int(row["balance_after"]) for row in recent] == [150, 120, 125, 120, -80]


@pytest.mark.asyncio
async def test_clear_receipt_keeps_existing_pin_behavior(
    ledger_db, monkeypatch, successful_context, owner_id, customer_id
):
    monkeypatch.setattr(jzbot, "db", ledger_db)
    await create_amount(ledger_db, owner_id, customer_id, source=301, amount=150)
    result = await ledger_db.clear_balance(
        owner_id, customer_id, 302, "business-1"
    )
    row = await ledger_db.get_outbox(result["outbox_id"])

    assert result["receipt_text"] == "余额已清零"
    assert row["receipt_text"] == "余额已清零"
    assert row["should_pin"] == 1

    await jzbot.deliver_outbox_receipt(successful_context, result["outbox_id"])
    assert successful_context.bot.send_message.await_args.kwargs["text"] == "余额已清零"
    successful_context.bot.pin_chat_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_balance", [0, 150, -80])
async def test_clear_receipt_preserves_history_idempotency_reply_and_retry(
    ledger_db, monkeypatch, successful_context, owner_id, customer_id,
    initial_balance,
):
    monkeypatch.setattr(jzbot, "db", ledger_db)
    if initial_balance:
        await create_amount(
            ledger_db, owner_id, customer_id, source=501, amount=initial_balance
        )
    before = [
        dict(row) for row in await ledger_db.get_recent_entries(
            owner_id, customer_id, limit=20
        )
    ]
    result = await ledger_db.clear_balance(
        owner_id, customer_id, 502, "business-1"
    )
    assert result["changed"] is bool(initial_balance)
    assert result["receipt_text"] == "余额已清零"
    assert await ledger_db.get_balance(owner_id, customer_id) == 0
    after = [
        dict(row) for row in await ledger_db.get_recent_entries(
            owner_id, customer_id, limit=20
        )
    ]
    assert after[:len(before)] == before
    assert len(after) == len(before) + bool(initial_balance)
    if initial_balance:
        assert after[-1]["entry_type"] == "clear"
        assert int(after[-1]["balance_after"]) == 0
        # Management history keeps its existing wording, not the receipt text.
        assert "账本已清空" in jzbot.admin_entry_line(after[-1])

    snapshot = {}
    for table in ("wallets", "ledger", "wallet_stats", "processed_messages",
                  "receipt_outbox"):
        snapshot[table] = [
            tuple(row) for row in await (
                await ledger_db.conn.execute(f"SELECT * FROM {table}")
            ).fetchall()
        ]
    duplicate = await ledger_db.clear_balance(
        owner_id, customer_id, 502, "business-1"
    )
    assert duplicate["duplicate"] is True
    assert duplicate["outbox_id"] == result["outbox_id"]
    for table, expected in snapshot.items():
        actual = await (
            await ledger_db.conn.execute(f"SELECT * FROM {table}")
        ).fetchall()
        assert [tuple(row) for row in actual] == expected

    successful_context.bot.send_message.side_effect = TelegramError("temporary")
    assert not await jzbot.deliver_outbox_receipt(
        successful_context, result["outbox_id"]
    )
    pending = await ledger_db.get_outbox(result["outbox_id"])
    assert pending["status"] == "pending"
    assert pending["receipt_text"] == "余额已清零"
    assert pending["attempt_count"] == 1
    successful_context.bot.send_message.side_effect = None
    assert await jzbot.deliver_outbox_receipt(
        successful_context, result["outbox_id"],
        now=datetime.fromisoformat(pending["next_attempt_at_utc"]),
    )
    delivered = await ledger_db.get_outbox(result["outbox_id"])
    assert delivered["status"] == "delivered"
    assert delivered["receipt_text"] == "余额已清零"
    for call in successful_context.bot.send_message.await_args_list:
        assert call.kwargs["text"] == "余额已清零"
        assert call.kwargs["reply_parameters"].message_id == 502
        assert call.kwargs["reply_parameters"].to_dict() == {"message_id": 502}
        assert "parse_mode" not in call.kwargs
        assert "rich_message" not in call.kwargs
        assert call.kwargs["business_connection_id"] == "business-1"
        assert call.kwargs["chat_id"] == customer_id
    assert successful_context.bot.pin_chat_message.await_count == bool(initial_balance)


@pytest.mark.asyncio
async def test_retry_uses_the_same_stored_receipt_text(
    ledger_db, monkeypatch, owner_id, customer_id
):
    monkeypatch.setattr(jzbot, "db", ledger_db)
    context = SimpleNamespace(
        bot=SimpleNamespace(
            send_message=AsyncMock(side_effect=TelegramError("temporary")),
            pin_chat_message=AsyncMock(),
        )
    )
    result = await create_amount(
        ledger_db, owner_id, customer_id, source=401, amount=150
    )

    assert not await jzbot.deliver_outbox_receipt(context, result["outbox_id"])
    pending = await ledger_db.get_outbox(result["outbox_id"])
    assert pending["receipt_text"] == "+15｜余15U"

    context.bot.send_message.side_effect = None
    context.bot.send_message.return_value = SimpleNamespace(message_id=555)
    assert await jzbot.deliver_outbox_receipt(
        context, result["outbox_id"],
        now=datetime.fromisoformat(pending["next_attempt_at_utc"]),
    )
    delivered = await ledger_db.get_outbox(result["outbox_id"])
    assert delivered["receipt_text"] == "+15｜余15U"
    assert context.bot.send_message.await_args.kwargs["text"] == "+15｜余15U"
