from types import SimpleNamespace

import pytest
import pytest_asyncio

import compat_api as jzbot


@pytest.fixture
def owner_id():
    return 1001


@pytest.fixture
def customer_id():
    return 2001


@pytest_asyncio.fixture
async def ledger_db(tmp_path, owner_id):
    database = jzbot.LedgerDB(tmp_path / "test-ledger.db")
    await database.open()
    await database.upsert_business_connection("business-1", owner_id, True)
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def successful_context():
    from unittest.mock import AsyncMock

    sent = SimpleNamespace(message_id=9876)
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=sent),
        pin_chat_message=AsyncMock(return_value=True),
    )
    return SimpleNamespace(bot=bot)
