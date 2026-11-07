"""Out-of-order delivery must never replace a newer pinned balance."""

from datetime import datetime
from types import SimpleNamespace

import pytest
from telegram.error import TelegramError

from jzbot.repositories.store import LedgerStore
from jzbot.services.outbox import OutboxService


class ReceiptBot:
    """Offline Telegram boundary tracking the actual text last pinned per chat."""

    def __init__(self):
        self.fail_replies = set()
        self.sent = []
        self.pins = {}
        self.pin_history = []

    async def send_message(self, **kwargs):
        source = kwargs["reply_parameters"].message_id
        if source in self.fail_replies:
            self.fail_replies.remove(source)
            raise TelegramError("temporary delivery failure")
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=len(self.sent))

    async def pin_chat_message(self, **kwargs):
        message = self.sent[kwargs["message_id"] - 1]
        key = (kwargs["business_connection_id"], kwargs["chat_id"])
        self.pins[key] = message["text"]
        self.pin_history.append(message["text"])


async def amount(store, owner, customer, source, value, connection="business-1"):
    return await store.apply_amount(owner, customer, source, value, connection)


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery_mode", ["direct", "batch"])
async def test_failed_older_receipt_cannot_replace_newer_pin(
    ledger_db, owner_id, customer_id, delivery_mode
):
    old = await amount(ledger_db, owner_id, customer_id, 11, 100)
    new = await amount(ledger_db, owner_id, customer_id, 12, 50)
    bot = ReceiptBot()
    bot.fail_replies.add(11)
    context = SimpleNamespace(bot=bot)
    service = OutboxService(ledger_db)

    if delivery_mode == "direct":
        assert not await service.deliver_outbox_receipt(context, old["outbox_id"])
        assert await service.deliver_outbox_receipt(context, new["outbox_id"])
    else:
        # Both rows were selected before the first send fails and enters backoff.
        assert await service.process_due_outbox(context) == 1
    assert bot.pin_history == ["入金 5 USDT, 余额 15 USDT"]

    pending = await ledger_db.get_outbox(old["outbox_id"])
    assert pending["status"] == "pending" and pending["attempt_count"] == 1
    assert not await service.deliver_outbox_receipt(context, old["outbox_id"])
    assert await service.deliver_outbox_receipt(
        context, old["outbox_id"],
        now=datetime.fromisoformat(pending["next_attempt_at_utc"]),
    )
    assert [message["text"] for message in bot.sent] == [
        "入金 5 USDT, 余额 15 USDT", "入金 10 USDT, 余额 10 USDT",
    ]
    assert bot.pin_history == ["入金 5 USDT, 余额 15 USDT"]
    assert bot.pins[("business-1", customer_id)] == "入金 5 USDT, 余额 15 USDT"
    assert [message["reply_parameters"].message_id for message in bot.sent] == [12, 11]

    duplicate = await amount(ledger_db, owner_id, customer_id, 11, 100)
    assert duplicate["duplicate"] and duplicate["outbox_id"] == old["outbox_id"]
    assert await service.deliver_outbox_receipt(context, old["outbox_id"])
    assert len(bot.sent) == 2
    assert await ledger_db.get_balance(owner_id, customer_id) == 150
    for table in ("ledger", "processed_messages", "receipt_outbox"):
        row = await (await ledger_db.conn.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
        assert row[0] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("other_owner", [False, True])
async def test_failure_and_pin_guard_are_isolated_by_owner_and_customer(
    ledger_db, owner_id, customer_id, other_owner
):
    old = await amount(ledger_db, owner_id, customer_id, 21, 100)
    next_owner = owner_id + int(other_owner)
    next_customer = customer_id if other_owner else customer_id + 1
    connection = "other-business" if other_owner else "business-1"
    if other_owner:
        await ledger_db.upsert_business_connection(connection, next_owner, True)
    await amount(ledger_db, next_owner, next_customer, 22, 50, connection)
    bot = ReceiptBot()
    bot.fail_replies.add(21)
    context = SimpleNamespace(bot=bot)
    service = OutboxService(ledger_db)

    assert await service.process_due_outbox(context) == 1
    assert bot.pins[(connection, next_customer)] == "入金 5 USDT, 余额 5 USDT"
    pending = await ledger_db.get_outbox(old["outbox_id"])
    assert await service.deliver_outbox_receipt(
        context, old["outbox_id"],
        now=datetime.fromisoformat(pending["next_attempt_at_utc"]),
    )
    assert bot.pins[("business-1", customer_id)] == "入金 10 USDT, 余额 10 USDT"
    assert bot.pin_history == ["入金 5 USDT, 余额 5 USDT", "入金 10 USDT, 余额 10 USDT"]


@pytest.mark.asyncio
async def test_newer_pending_receipt_does_not_suppress_normal_pin(
    ledger_db, owner_id, customer_id
):
    old = await amount(ledger_db, owner_id, customer_id, 31, 100)
    new = await amount(ledger_db, owner_id, customer_id, 32, 50)
    bot = ReceiptBot()
    context = SimpleNamespace(bot=bot)
    service = OutboxService(ledger_db)

    assert await service.deliver_outbox_receipt(context, old["outbox_id"])
    assert bot.pin_history == ["入金 10 USDT, 余额 10 USDT"]
    assert await service.deliver_outbox_receipt(context, new["outbox_id"])
    assert bot.pin_history == ["入金 10 USDT, 余额 10 USDT", "入金 5 USDT, 余额 15 USDT"]


@pytest.mark.asyncio
async def test_newer_clear_pin_survives_older_deposit_delivery(
    ledger_db, owner_id, customer_id
):
    old = await amount(ledger_db, owner_id, customer_id, 41, 100)
    clear = await ledger_db.clear_balance(owner_id, customer_id, 42, "business-1")
    bot = ReceiptBot()
    context = SimpleNamespace(bot=bot)
    service = OutboxService(ledger_db)

    assert await service.deliver_outbox_receipt(context, clear["outbox_id"])
    assert await service.deliver_outbox_receipt(context, old["outbox_id"])
    assert [message["text"] for message in bot.sent] == [
        "余额已清零", "入金 10 USDT, 余额 10 USDT",
    ]
    assert bot.pin_history == ["余额已清零"]
    assert await ledger_db.get_balance(owner_id, customer_id) == 0


@pytest.mark.asyncio
async def test_newer_noop_clear_does_not_change_pin_semantics(
    ledger_db, owner_id, customer_id
):
    await amount(ledger_db, owner_id, customer_id, 51, 100)
    withdrawal = await amount(ledger_db, owner_id, customer_id, 52, -100)
    clear = await ledger_db.clear_balance(owner_id, customer_id, 53, "business-1")
    bot = ReceiptBot()
    context = SimpleNamespace(bot=bot)
    service = OutboxService(ledger_db)

    assert await service.deliver_outbox_receipt(context, clear["outbox_id"])
    assert bot.pin_history == []
    assert await service.deliver_outbox_receipt(context, withdrawal["outbox_id"])
    assert bot.pin_history == ["出金 10 USDT, 余额 0 USDT"]


@pytest.mark.asyncio
async def test_newer_pin_guard_survives_restart_and_keeps_legacy_receipt_text(
    tmp_path, owner_id, customer_id
):
    path = tmp_path / "receipt-order-restart.db"
    first = LedgerStore(path)
    await first.open()
    bot = ReceiptBot()
    context = SimpleNamespace(bot=bot)
    try:
        await first.upsert_business_connection("business-1", owner_id, True)
        old = await amount(first, owner_id, customer_id, 61, 100)
        new = await amount(first, owner_id, customer_id, 62, 50)
        await first.conn.execute(
            "UPDATE receipt_outbox SET receipt_text=? WHERE id=?",
            ("+10｜余10U", old["outbox_id"]),
        )
        await first.conn.commit()
        assert await OutboxService(first).deliver_outbox_receipt(context, new["outbox_id"])
    finally:
        await first.close()

    restarted = LedgerStore(path)
    await restarted.open()
    try:
        assert await OutboxService(restarted).process_due_outbox(context) == 1
        assert bot.sent[-1]["text"] == "+10｜余10U"
        assert bot.pin_history == ["入金 5 USDT, 余额 15 USDT"]
        row = await restarted.get_outbox(old["outbox_id"])
        assert row["status"] == "delivered" and row["receipt_text"] == "+10｜余10U"
    finally:
        await restarted.close()
