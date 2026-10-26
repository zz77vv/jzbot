"""Test-only bridge retaining V1 assertions against the real V2 components.

No production module imports this adapter or V1. Tests install their own temporary
LedgerStore; all invocations go through V2's injected handlers/services. The legacy
publisher tests are intentionally not reused for the unrelated V2 publisher.
"""
from pathlib import Path

from jzbot.config import Settings
from jzbot.runtime import Runtime
from jzbot.repositories.store import LedgerStore as LedgerDB
from jzbot.app import build_application as assemble
from jzbot.domain import (
    ADMIN_PAGE_SIZE, LocateFailureCategory, LocateResult, classify_locate_error,
    format_tenths, locate_failure_message, parse_amount_to_tenths, utc_now_iso,
)
from jzbot.ui.pages import (
    InlineKeyboardButton, admin_button, admin_entry_line, admin_rich_table,
    build_customer_detail_view, build_customer_history_view, build_customer_list_view,
    build_home_view, build_outbox_attention_view, build_search_results_view,
    customer_button_label, customer_summary_text, safe_edit_query,
)

db = None


def settings_for(path):
    return Settings(str(123456789) + ":" + "X" * 35, Path(path),
                    Path(path).parent / ".test.instance.lock",
                    Path(path).parent / "test.log", False, "offline-test")


def active():
    if db is None:
        raise AssertionError("Install a temporary test store before calling the adapter")
    if not hasattr(db, "test_runtime"):
        db.test_runtime = Runtime(settings_for(db.db_path), store=db)
    return db.test_runtime


def build_application():
    settings = settings_for(Path(__file__).parent / "not-opened.db")
    return assemble(settings)


async def on_business_message(*args, **kwargs):
    return await active().business.on_business_message(*args, **kwargs)


async def on_admin_callback(*args, **kwargs):
    return await active().admin.on_admin_callback(*args, **kwargs)


async def on_admin_search_text(*args, **kwargs):
    return await active().admin.on_admin_search_text(*args, **kwargs)


async def on_start(*args, **kwargs):
    return await active().admin.on_start(*args, **kwargs)


async def get_admin_home_view(*args, **kwargs):
    return await active().admin.get_admin_home_view(*args, **kwargs)


async def get_admin_search_view(*args, **kwargs):
    return await active().admin.get_admin_search_view(*args, **kwargs)


async def locate_customer_chat(*args, **kwargs):
    return await active().connections.locate_customer_chat(*args, **kwargs)


async def deliver_outbox_receipt(*args, **kwargs):
    return await active().outbox.deliver_outbox_receipt(*args, **kwargs)


async def process_due_outbox(*args, **kwargs):
    return await active().outbox.process_due_outbox(*args, **kwargs)
