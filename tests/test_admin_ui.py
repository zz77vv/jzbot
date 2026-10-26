from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

import compat_api as jzbot


def wallet(customer=2001, balance=0, name="张三", username=None, updated="2026-10-10"):
    return {
        "customer_chat_id": customer, "balance_tenths": str(balance),
        "first_name": name, "last_name": None, "username": username,
        "updated_at": updated,
    }


def buttons(keyboard):
    return [button for row in keyboard.inline_keyboard for button in row]


def callbacks(keyboard):
    return [button.callback_data for button in buttons(keyboard) if button.callback_data]


class AdminHTMLValidator(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        assert tag in {"b", "h2", "h3", "p", "br", "hr", "footer", "details", "summary", "table", "tr", "th", "td", "tg-button", "tg-button-row"}
        if tag == "table":
            assert attrs == [("bordered", None), ("striped", None), ("compact", None)]
        elif tag == "tg-button":
            assert dict(attrs)["type"] in {"callback_data", "url", "copy_text"}
        elif tag == "tg-button-row":
            assert attrs == [("align", "left")]
        else:
            assert not attrs
        if tag in {"br", "hr"}:
            return
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in {'br', 'hr'}:
            return
        assert self.stack.pop() == tag

    def handle_data(self, data):
        self.text.append(data)


class RichControlsParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.attrs = None

    def handle_starttag(self, tag, attrs):
        if tag == 'tg-button-row':
            self.rows.append([])
        elif tag == 'tg-button':
            self.attrs = dict(attrs)
            self.label = ''

    def handle_data(self, data):
        if self.attrs is not None:
            self.label += data

    def handle_endtag(self, tag):
        if tag != 'tg-button':
            return
        attrs = self.attrs
        action = attrs['type']
        kwargs = {'style': attrs.get('style')}
        if action == 'callback_data':
            kwargs['callback_data'] = attrs['data']
        elif action == 'url':
            kwargs['url'] = attrs['url']
        else:
            kwargs['copy_text'] = CopyTextButton(attrs['text'])
        self.rows[-1].append(InlineKeyboardButton(self.label, **kwargs))
        self.attrs = None


def rich_controls(html):
    parser = RichControlsParser()
    parser.feed(html)
    return InlineKeyboardMarkup(parser.rows)


def check_view(text, keyboard):
    parser = AdminHTMLValidator()
    parser.feed(text)
    parser.close()
    assert not parser.stack
    assert len(''.join(parser.text).encode("utf-16-le")) // 2 < 4096
    assert "USDT" not in text
    assert all(1 <= len(data.encode("utf-8")) <= 64 for data in callbacks(keyboard))


async def add(database, owner, customer, source=1, amount=10, name="张三", username=None):
    await database.upsert_customer_profile(owner, customer, username, name, None)
    return await database.apply_amount(owner, customer, source, amount, "business-1")


def callback_update(data, owner):
    bot = SimpleNamespace(_post=AsyncMock(return_value={"message_id": 20}))
    query = SimpleNamespace(
        data=data, answer=AsyncMock(), get_bot=Mock(return_value=bot),
        message=SimpleNamespace(chat_id=owner, message_id=20),
        inline_message_id=None,
    )
    return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=owner))


def rendered_query(query):
    data = query.get_bot()._post.await_args.kwargs["data"]
    return {
        "text": data["rich_message"]["html"],
        "reply_markup": rich_controls(data["rich_message"]["html"]),
    }


async def snapshot(database):
    result = {}
    for table in ("wallets", "ledger", "wallet_stats", "processed_messages", "receipt_outbox"):
        rows = await (await database.conn.execute(f"SELECT * FROM {table} ORDER BY rowid")).fetchall()
        result[table] = [tuple(row) for row in rows]
    return result


def test_home_counts_net_and_styles():
    rows = [wallet(1, 320), wallet(2, 150), wallet(3, 224), wallet(4, -110), wallet(5, 0)]
    text, keyboard = jzbot.build_home_view(rows)
    assert "<b>58.4 U</b>" in text
    assert "<td>客户总数</td><td><b>5 人</b></td>" in text
    assert "<td>存款用户</td><td><b>3 人</b></td>" in text
    assert "<td>欠款用户</td><td><b>1 人</b></td>" in text
    assert "<td>零余额用户</td><td><b>1 人</b></td>" in text
    assert "<td>存款总额</td><td><b>69.4 U</b></td>" in text
    assert "<td>欠款总额</td><td><b>11 U</b></td>" in text
    assert "<table bordered striped compact>" in text and text.count("<tr>") == 7
    assert "outbox:0" not in callbacks(keyboard)
    assert keyboard.inline_keyboard[0][0].style == "success"
    assert keyboard.inline_keyboard[0][1].style == "danger"
    assert keyboard.inline_keyboard[1][1].style == "primary"
    check_view(text, keyboard)


def test_empty_home_and_optional_attention_entry():
    text, keyboard = jzbot.build_home_view([])
    assert "<b>0 U</b>" in text and "<td>客户总数</td><td><b>0 人</b></td>" in text
    assert not any("异常" in button.text for button in buttons(keyboard))
    _, attention_keyboard = jzbot.build_home_view([], 2)
    assert any(button.text == "⚠️ 回执异常（2）" for button in buttons(attention_keyboard))
    check_view(text, keyboard)


@pytest.mark.parametrize(("kind", "expected"), [("p", [2, 1]), ("d", [4, 3]), ("a", [4, 3, 2, 1])])
def test_list_sorting_preserves_existing_rules(kind, expected):
    rows = [wallet(1, 5, updated="1"), wallet(2, 150, updated="2"),
            wallet(3, -10, updated="3"), wallet(4, -110, updated="4")]
    text, keyboard = jzbot.build_customer_list_view(rows, kind, 0)
    customer_ids = [int(data.split(":")[1]) for data in callbacks(keyboard) if data.startswith("user:")]
    assert customer_ids == expected
    if kind == "p":
        assert any(button.text.endswith("｜+15U") for button in buttons(keyboard))
    check_view(text, keyboard)


def test_list_ten_per_page_and_last_page_boundaries():
    rows = [wallet(index, index + 1) for index in range(11)]
    text, keyboard = jzbot.build_customer_list_view(rows, "p", 0)
    assert len([data for data in callbacks(keyboard) if data.startswith("user:")]) == 10
    assert "list:p:1" in callbacks(keyboard)
    assert "list:p:-1" not in callbacks(keyboard)
    assert "第 1 / 2 页" in text
    last_text, last = jzbot.build_customer_list_view(rows, "p", 999)
    assert len([data for data in callbacks(last) if data.startswith("user:")]) == 1
    assert "list:p:0" in callbacks(last) and "list:p:2" not in callbacks(last)
    assert "第 2 / 2 页" in last_text
    assert "home" in callbacks(last)
    check_view(last_text, last)


def test_long_customer_button_keeps_balance_and_identity():
    row = wallet(9876543210, -125, name="非常长的中文客户姓名" * 8)
    label = jzbot.customer_button_label(row)
    assert "…" in label and "9876543210" in label and label.endswith("｜-12.5U")


@pytest.mark.asyncio
async def test_customer_detail_html_balance_stats_recent_and_links(ledger_db, owner_id, customer_id):
    await add(ledger_db, owner_id, customer_id, 1, 150, name="张<&三>", username="unsafe<script>")
    await add(ledger_db, owner_id, customer_id, 2, -30, name="张<&三>", username="unsafe<script>")
    detail = await ledger_db.get_customer_detail(owner_id, customer_id)
    text, keyboard = jzbot.build_customer_detail_view(detail, "s", 1)
    assert "张&lt;&amp;三&gt;" in text and "@unsafe&lt;script&gt;" in text
    assert "<b>12 U</b>" in text and "<b>15 U</b>" in text and "<b>3 U</b>" in text
    assert "<td>记账次数</td><td><b>2 笔</b></td>" in text
    assert text.index("-3｜余12U") < text.index("+15｜余15U")
    assert not any(button.url for button in buttons(keyboard))
    assert f"history:{customer_id}:0:s:1" in callbacks(keyboard)
    assert "search_page:1" in callbacks(keyboard)
    check_view(text, keyboard)


@pytest.mark.asyncio
async def test_detail_recent_five_and_public_username_buttons(ledger_db, owner_id, customer_id):
    for index in range(6):
        await add(ledger_db, owner_id, customer_id, index + 1, 10, username="public_user")
    detail = await ledger_db.get_customer_detail(owner_id, customer_id)
    text, keyboard = jzbot.build_customer_detail_view(detail, "a", 0)
    assert len(detail["recent"]) == 5
    assert "最近 5 笔" in text and "+1｜余1U" not in text
    chat_row = next(row for row in keyboard.inline_keyboard if any(b.url for b in row))
    assert chat_row[0].url == "https://t.me/public_user"
    assert chat_row[1].callback_data == f"locate:{customer_id}:a:0"
    check_view(text, keyboard)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 1, 10, 11, 100])
async def test_history_database_pagination_and_latest_first(ledger_db, owner_id, customer_id, count):
    # Seed an existing empty wallet in the test DB only.
    timestamp = jzbot.utc_now_iso()
    await ledger_db.conn.execute(
        "INSERT INTO wallets(owner_user_id, customer_chat_id, balance_tenths, "
        "created_at, updated_at) VALUES (?, ?, '0', ?, ?)",
        (owner_id, customer_id, timestamp, timestamp),
    )
    await ledger_db.conn.commit()
    # Clearing zero must not invent a history entry.
    await ledger_db.clear_balance(owner_id, customer_id, 9000, "business-1")
    for index in range(count):
        await add(ledger_db, owner_id, customer_id, index + 1, 10)
    pages = max(1, (count + 9) // 10)
    detail = await ledger_db.get_customer_detail(owner_id, customer_id)
    seen_balances = []
    for target in range(pages):
        entries, total, actual = await ledger_db.get_ledger_page(owner_id, customer_id, target)
        assert total == count and actual == target
        assert len(entries) <= 10
        seen_balances.extend(int(row["balance_after"]) for row in entries)
        text, keyboard = jzbot.build_customer_history_view(detail, entries, total, actual, "a", 0)
        assert f"第 {target + 1} / {pages} 页" in text
        if target + 1 == pages:
            assert not any(data.startswith(f"history:{customer_id}:{pages}:") for data in callbacks(keyboard))
        check_view(text, keyboard)
        if count == 0:
            assert "暂无流水记录" in text
    assert seen_balances == list(range(count * 10, 0, -10))
    _, total, clamped_page = await ledger_db.get_ledger_page(owner_id, customer_id, 999999)
    assert total == count and clamped_page == pages - 1


@pytest.mark.asyncio
async def test_history_uses_old_balance_and_includes_clear(ledger_db, owner_id, customer_id):
    await add(ledger_db, owner_id, customer_id, 1, 150)
    await add(ledger_db, owner_id, customer_id, 2, -30)
    await ledger_db.clear_balance(owner_id, customer_id, 3, "business-1")
    detail = await ledger_db.get_customer_detail(owner_id, customer_id)
    entries, total, page = await ledger_db.get_ledger_page(owner_id, customer_id)
    text, keyboard = jzbot.build_customer_history_view(detail, entries, total, page, "p", 0)
    assert "当前余额：<b>0 U</b>" in text
    assert "+15｜余15U" in text and "-3｜余12U" in text and "账本已清空" in text
    assert [row["entry_type"] for row in entries] == ["clear", "withdraw", "deposit"]
    check_view(text, keyboard)


@pytest.mark.asyncio
async def test_history_query_uses_index_limit_offset(ledger_db, owner_id, customer_id):
    statements = []
    await ledger_db.conn.set_trace_callback(statements.append)
    await ledger_db.get_ledger_page(owner_id, customer_id, 0)
    assert any("COUNT(*)" in sql and "owner_user_id" in sql and "customer_chat_id" in sql for sql in statements)
    assert any("ORDER BY id DESC LIMIT 10 OFFSET 0" in sql for sql in statements)
    plan = await (await ledger_db.conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM ledger WHERE owner_user_id=? "
        "AND customer_chat_id=? ORDER BY id DESC LIMIT 10 OFFSET 0", (owner_id, customer_id)
    )).fetchall()
    assert any("idx_ledger_recent" in str(tuple(row)) for row in plan)


@pytest.mark.asyncio
async def test_history_owner_isolation_and_forged_callback(ledger_db, monkeypatch, owner_id, customer_id):
    other_owner = owner_id + 1
    await ledger_db.upsert_business_connection("other", other_owner, True)
    await add(ledger_db, other_owner, customer_id, 1, 999)
    rows, total, _ = await ledger_db.get_ledger_page(owner_id, customer_id)
    assert rows == [] and total == 0
    monkeypatch.setattr(jzbot, "db", ledger_db)
    history_read = AsyncMock(wraps=ledger_db.get_ledger_page)
    monkeypatch.setattr(ledger_db, "get_ledger_page", history_read)
    update = callback_update(f"history:{customer_id}:0:a:0", owner_id)
    await jzbot.on_admin_callback(update, SimpleNamespace(user_data={}))
    history_read.assert_not_awaited()
    assert "客户账本不存在" in rendered_query(update.callback_query)["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["history:bad:0:a:0", "history:2001", "user:bad:a:0", "list:p"])
async def test_malformed_callbacks_are_friendly(ledger_db, monkeypatch, owner_id, data):
    monkeypatch.setattr(jzbot, "db", ledger_db)
    update = callback_update(data, owner_id)
    await jzbot.on_admin_callback(update, SimpleNamespace(user_data={}))
    update.callback_query.answer.assert_awaited_once()
    assert "首页" in rendered_query(update.callback_query)["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["@", "", "   ", " @ ", "a" * 65])
async def test_invalid_search_never_returns_all_customers(ledger_db, owner_id, customer_id, query):
    await add(ledger_db, owner_id, customer_id)
    assert await ledger_db.search_customers(owner_id, query) == ([], 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["@", "   ", "a" * 65])
async def test_invalid_search_input_keeps_state_for_retry(ledger_db, monkeypatch, owner_id, text):
    monkeypatch.setattr(jzbot, "db", ledger_db)
    update = SimpleNamespace(message=SimpleNamespace(text=text, reply_text=AsyncMock()),
                             effective_user=SimpleNamespace(id=owner_id),
                             effective_chat=SimpleNamespace(type="private", id=owner_id))
    context = SimpleNamespace(user_data={"awaiting_customer_search": True},
                              bot=SimpleNamespace(_post=AsyncMock(return_value={"message_id": 1})))
    await jzbot.on_admin_search_text(update, context)
    assert context.user_data["awaiting_customer_search"] is True
    assert "customer_search_query" not in context.user_data
    context.bot._post.assert_awaited_once()
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_business_customer_messages_cannot_enter_search(ledger_db, monkeypatch, owner_id):
    monkeypatch.setattr(jzbot, "db", ledger_db)
    update = SimpleNamespace(message=None, business_message=SimpleNamespace(text="张三"),
                             effective_user=SimpleNamespace(id=owner_id),
                             effective_chat=SimpleNamespace(type="private"))
    context = SimpleNamespace(user_data={"awaiting_customer_search": True})
    await jzbot.on_admin_search_text(update, context)
    assert "customer_search_query" not in context.user_data


@pytest.mark.asyncio
async def test_search_context_pagination_restart_cancel_and_expiration(ledger_db, monkeypatch, owner_id):
    monkeypatch.setattr(jzbot, "db", ledger_db)
    for index in range(11):
        await add(ledger_db, owner_id, 3000 + index, index + 1, name="中文 Search")
    context = SimpleNamespace(user_data={"customer_search_query": "中文"})
    update = callback_update("search_page:1", owner_id)
    await jzbot.on_admin_callback(update, context)
    rendered = rendered_query(update.callback_query)
    assert "第 2 / 2 页" in rendered["text"] and "找到 11 位客户" in rendered["text"]
    assert context.user_data["customer_search_query"] == "中文"
    assert len([data for data in callbacks(rendered["reply_markup"]) if data.startswith("user:")]) == 1
    for data in ("search", "search_cancel", "search_page:0"):
        update = callback_update(data, owner_id)
        await jzbot.on_admin_callback(update, context)
        if data == "search":
            assert context.user_data["awaiting_customer_search"] is True
            assert "customer_search_query" not in context.user_data
        elif data == "search_cancel":
            assert "awaiting_customer_search" not in context.user_data
            assert "账本管理" in rendered_query(update.callback_query)["text"]
        else:
            assert "搜索已过期" in rendered_query(update.callback_query)["text"]


@pytest.mark.asyncio
async def test_search_special_html_and_clamped_page(ledger_db, monkeypatch, owner_id, customer_id):
    await add(ledger_db, owner_id, customer_id, name="A<&>中文")
    monkeypatch.setattr(jzbot, "db", ledger_db)
    text, keyboard = await jzbot.get_admin_search_view(owner_id, "<&>", 999999)
    assert "关键词：&lt;&amp;&gt;" in text
    assert "第 1 / 1 页" in text
    assert len([data for data in callbacks(keyboard) if data.startswith("user:")]) == 1
    check_view(text, keyboard)


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "attempts", "age_minutes", "expected"), [
    ("pending", 0, 0, 0), ("pending", 1, 1, 0),
    ("pending", 3, 0, 1), ("pending", 0, 16, 1),
    ("delivered", 8, 100, 0),
])
async def test_outbox_attention_thresholds(ledger_db, owner_id, customer_id, status, attempts, age_minutes, expected):
    result = await add(ledger_db, owner_id, customer_id)
    now = datetime(2026, 10, 11, 8, 0, tzinfo=timezone.utc)
    created = now - timedelta(minutes=age_minutes)
    await ledger_db.conn.execute(
        "UPDATE receipt_outbox SET status=?, attempt_count=?, created_at_utc=? WHERE id=?",
        (status, attempts, created.isoformat(), result["outbox_id"]),
    )
    await ledger_db.conn.commit()
    assert await ledger_db.get_outbox_attention_count(owner_id, now) == expected
    entries, total, page = await ledger_db.get_outbox_attention_page(owner_id, now=now)
    assert total == expected and len(entries) == expected and page == 0


@pytest.mark.asyncio
async def test_outbox_owner_isolation_pagination_redaction_and_readonly(ledger_db, owner_id, customer_id):
    now = datetime.now(timezone.utc)
    other_owner = owner_id + 1
    await ledger_db.upsert_business_connection("other", other_owner, True)
    for index in range(12):
        await add(ledger_db, owner_id, customer_id + index, index + 1, name="异常<&客户")
    await add(ledger_db, other_owner, 888888, 99, name="PRIVATE OTHER OWNER")
    await ledger_db.conn.execute(
        "UPDATE receipt_outbox SET attempt_count=3, last_error=?",
        ("https://api.telegram.org/botSECRET_CREDENTIAL/sendMessage unknown error",),
    )
    await ledger_db.conn.commit()
    before = await snapshot(ledger_db)
    first, total, page = await ledger_db.get_outbox_attention_page(owner_id, now=now)
    assert len(first) == 10 and total == 12
    text, keyboard = jzbot.build_outbox_attention_view(first, total, page)
    assert "≥3" in text and "≥15" in text
    assert "仅供查看，自动重试仍在继续" in text
    assert "SECRET" not in text and "PRIVATE OTHER" not in text
    assert "异常&lt;&amp;客户" in text
    assert all(data.startswith("outbox:") or data == "home" for data in callbacks(keyboard))
    last, total, page = await ledger_db.get_outbox_attention_page(owner_id, 1, now)
    assert len(last) == 2 and page == 1 and total == 12
    assert await snapshot(ledger_db) == before
    check_view(text, keyboard)


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["home", "list:a:0", "user:2001:a:0", "history:2001:0:a:0", "search_page:0", "outbox:0"])
async def test_navigation_answers_before_loading_and_edits_existing_message(ledger_db, monkeypatch, owner_id, customer_id, data):
    await add(ledger_db, owner_id, customer_id)
    monkeypatch.setattr(jzbot, "db", ledger_db)
    update = callback_update(data, owner_id)
    query = update.callback_query
    original_edit = query.get_bot()._post

    async def checked_edit(method, *, data):
        query.answer.assert_awaited_once()
        assert method == "editMessageText"
        assert "parse_mode" not in data and "text" not in data
        assert data["chat_id"] == owner_id and data["message_id"] == 20
        await original_edit(method, data=data)
        rendered = rendered_query(query)
        check_view(rendered["text"], rendered["reply_markup"])
        return {"message_id": 20}

    query.get_bot()._post = AsyncMock(side_effect=checked_edit)
    context = SimpleNamespace(user_data={"customer_search_query": "张三"})
    for method_name in ("get_wallet_rows", "get_customer_detail", "search_customers", "get_outbox_attention_page"):
        method = getattr(ledger_db, method_name)

        async def checked_read(*args, _method=method, **kwargs):
            query.answer.assert_awaited_once()
            return await _method(*args, **kwargs)

        monkeypatch.setattr(ledger_db, method_name, checked_read)
    before = await snapshot(ledger_db)
    await jzbot.on_admin_callback(update, context)
    query.answer.assert_awaited_once()
    original_edit.assert_awaited_once()
    assert await snapshot(ledger_db) == before


@pytest.mark.asyncio
async def test_start_home_uses_html_and_fresh_pending_does_not_warn(ledger_db, monkeypatch, owner_id, customer_id):
    await add(ledger_db, owner_id, customer_id)
    monkeypatch.setattr(jzbot, "db", ledger_db)
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()),
                             effective_user=SimpleNamespace(id=owner_id),
                             effective_chat=SimpleNamespace(type="private", id=owner_id))
    context = SimpleNamespace(user_data={}, bot=SimpleNamespace(_post=AsyncMock(return_value={"message_id": 1})))
    await jzbot.on_start(update, context)
    assert context.bot._post.await_args.args[0] == "sendRichMessage"
    data = context.bot._post.await_args.kwargs["data"]
    assert "parse_mode" not in data and "text" not in data
    assert "<table bordered striped compact>" in data["rich_message"]["html"]
    assert "outbox:0" not in callbacks(rich_controls(data["rich_message"]["html"]))
    assert data["reply_markup"]["inline_keyboard"] == []
    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_safe_edit_ignores_only_not_modified():
    query = callback_update("home", 1001).callback_query
    query.get_bot()._post.side_effect = BadRequest("message is not modified")
    _, keyboard = jzbot.build_home_view([])
    await jzbot.safe_edit_query(query, "unchanged", keyboard)
    query.get_bot()._post.side_effect = BadRequest("real edit failure")
    with pytest.raises(BadRequest, match="real edit failure"):
        await jzbot.safe_edit_query(query, "changed", keyboard)


def test_native_buttons_fallback_for_older_ptb(monkeypatch):
    original = jzbot.InlineKeyboardButton

    def legacy_button(text, callback_data):
        return original(text, callback_data=callback_data)

    monkeypatch.setattr(__import__("jzbot.ui.pages", fromlist=["InlineKeyboardButton"]), "InlineKeyboardButton", legacy_button)
    text, keyboard = jzbot.build_home_view([])
    assert "list:p:0" in callbacks(keyboard)
    assert all(button.style is None for button in buttons(keyboard))
    check_view(text, keyboard)


def test_callback_byte_limit_is_enforced():
    assert jzbot.admin_button("safe", "x" * 64).callback_data == "x" * 64
    with pytest.raises(ValueError):
        jzbot.admin_button("invalid", "中" * 22)


@pytest.mark.asyncio
async def test_large_profiles_are_bounded_on_mobile_pages(ledger_db, owner_id, customer_id):
    for index in range(10):
        await add(ledger_db, owner_id, customer_id + index, index + 1,
                  name="<&中文客户>" * 100, username="public_user")
    await ledger_db.conn.execute(
        "UPDATE receipt_outbox SET attempt_count=3, last_error='BUSINESS_CONNECTION_INVALID'"
    )
    await ledger_db.conn.commit()
    rows, total, page = await ledger_db.get_outbox_attention_page(owner_id)
    text, keyboard = jzbot.build_outbox_attention_view(rows, total, page)
    assert "Business 连接失效" in text
    check_view(text, keyboard)
    detail = await ledger_db.get_customer_detail(owner_id, customer_id)
    check_view(*jzbot.build_customer_detail_view(detail, "a", 0))


@pytest.mark.asyncio
async def test_attention_home_entry_tracks_delivery_without_changing_retries(ledger_db, monkeypatch, owner_id, customer_id):
    result = await add(ledger_db, owner_id, customer_id)
    await ledger_db.conn.execute("UPDATE receipt_outbox SET attempt_count=3 WHERE id=?", (result["outbox_id"],))
    await ledger_db.conn.commit()
    monkeypatch.setattr(jzbot, "db", ledger_db)
    _, keyboard = await jzbot.get_admin_home_view(owner_id)
    assert "outbox:0" in callbacks(keyboard)
    await ledger_db.mark_outbox_delivered(result["outbox_id"], 12345)
    _, keyboard = await jzbot.get_admin_home_view(owner_id)
    assert "outbox:0" not in callbacks(keyboard)
