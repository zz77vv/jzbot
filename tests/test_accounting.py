from types import SimpleNamespace

import pytest

import compat_api as jzbot


async def apply(database, owner, customer, source, amount):
    return await database.apply_amount(
        owner,
        customer,
        source,
        amount,
        "business-1",
    )


@pytest.mark.parametrize(
    ("text", "tenths"),
    [("+10", 100), ("-10", -100), ("+0.5", 5)],
)
def test_integer_amount_parsing(text, tenths):
    assert jzbot.parse_amount_to_tenths(text) == tenths


@pytest.mark.asyncio
async def test_deposit_withdraw_fraction_and_stats(
    ledger_db, owner_id, customer_id
):
    await apply(ledger_db, owner_id, customer_id, 1, 100)
    await apply(ledger_db, owner_id, customer_id, 2, -100)
    await apply(ledger_db, owner_id, customer_id, 3, 5)

    assert await ledger_db.get_balance(owner_id, customer_id) == 5
    detail = await ledger_db.get_customer_detail(owner_id, customer_id)
    assert detail["deposit_total"] == 105
    assert detail["withdraw_total"] == 100
    assert detail["entry_count"] == 3

    entries = await ledger_db.get_recent_entries(owner_id, customer_id, limit=20)
    assert len(entries) == 3
    assert sum(
        int(row["amount_tenths"])
        for row in entries
        if row["entry_type"] == "deposit"
    ) == detail["deposit_total"]
    assert sum(
        int(row["amount_tenths"])
        for row in entries
        if row["entry_type"] == "withdraw"
    ) == detail["withdraw_total"]


@pytest.mark.asyncio
async def test_duplicate_source_is_exactly_once(ledger_db, owner_id, customer_id):
    first = await apply(ledger_db, owner_id, customer_id, 10, 100)
    duplicate = await apply(ledger_db, owner_id, customer_id, 10, 100)

    assert first["duplicate"] is False
    assert duplicate["duplicate"] is True
    assert duplicate["outbox_id"] == first["outbox_id"]
    assert await ledger_db.get_balance(owner_id, customer_id) == 100
    ledger_count = await (
        await ledger_db.conn.execute("SELECT COUNT(*) AS count FROM ledger")
    ).fetchone()
    outbox_count = await (
        await ledger_db.conn.execute("SELECT COUNT(*) AS count FROM receipt_outbox")
    ).fetchone()
    assert ledger_count["count"] == 1
    assert outbox_count["count"] == 1


@pytest.mark.asyncio
async def test_clear_and_zero_clear_semantics(ledger_db, owner_id, customer_id):
    await apply(ledger_db, owner_id, customer_id, 20, 250)
    changed = await ledger_db.clear_balance(
        owner_id, customer_id, 21, "business-1"
    )
    unchanged = await ledger_db.clear_balance(
        owner_id, customer_id, 22, "business-1"
    )

    assert changed["changed"] is True
    assert unchanged["changed"] is False
    assert await ledger_db.get_balance(owner_id, customer_id) == 0
    ledger_count = await (
        await ledger_db.conn.execute("SELECT COUNT(*) AS count FROM ledger")
    ).fetchone()
    processed_count = await (
        await ledger_db.conn.execute(
            "SELECT COUNT(*) AS count FROM processed_messages"
        )
    ).fetchone()
    outbox_count = await (
        await ledger_db.conn.execute("SELECT COUNT(*) AS count FROM receipt_outbox")
    ).fetchone()
    assert ledger_count["count"] == 2
    assert processed_count["count"] == 3
    assert outbox_count["count"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("commands", "expected"),
    [
        (("+100", "//"), 0),
        (("//", "+100"), 1000),
        (("+100", "-30", "//"), 0),
    ],
)
async def test_financial_commands_run_in_order(
    ledger_db, owner_id, customer_id, commands, expected
):
    for source, command in enumerate(commands, start=100):
        if command == "//":
            await ledger_db.clear_balance(
                owner_id, customer_id, source, "business-1"
            )
        else:
            await apply(
                ledger_db,
                owner_id,
                customer_id,
                source,
                jzbot.parse_amount_to_tenths(command),
            )
    assert await ledger_db.get_balance(owner_id, customer_id) == expected


@pytest.mark.asyncio
async def test_edited_business_message_cannot_change_accounting(
    ledger_db, monkeypatch, owner_id, customer_id
):
    monkeypatch.setattr(jzbot, "db", ledger_db)
    update = SimpleNamespace(business_message=None, edited_business_message=object())
    await jzbot.on_business_message(update, SimpleNamespace())
    assert await ledger_db.get_balance(owner_id, customer_id) == 0


def test_application_is_strictly_sequential():
    application = jzbot.build_application()
    assert application.update_processor.max_concurrent_updates == 1


def test_admin_search_handler_only_accepts_ordinary_messages():
    application = jzbot.build_application()
    search_handlers = [
        handler
        for handler in application.handlers[0]
        if getattr(handler, "callback", None) .__name__ == "on_admin_search_text"
    ]
    assert len(search_handlers) == 1
    assert "UpdateType.MESSAGE" in repr(search_handlers[0].filters)


def test_production_paths_require_explicit_existing_database(tmp_path, monkeypatch):
    from jzbot.config import Settings
    monkeypatch.delenv("JZBOT_CONFIG", raising=False)
    monkeypatch.delenv("JZBOT_DB_PATH", raising=False)
    monkeypatch.setenv("JZBOT_TOKEN", str(123456789) + ":" + "X" * 35)
    with pytest.raises(ValueError, match="JZBOT_DB_PATH"):
        Settings.from_environment()
    path = tmp_path / "ledger.db"
    path.touch()
    monkeypatch.setenv("JZBOT_DB_PATH", str(path))
    configured = Settings.from_environment()
    assert configured.db_path == path.resolve()
    assert configured.lock_path == path.parent / ".jzbot.instance.lock"
    assert configured.require_existing_db
