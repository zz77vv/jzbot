from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest

import compat_api as jzbot
from jzbot.infra.rich import send_admin_rich
from test_admin_ui import (
    add, buttons, callback_update, callbacks, check_view, rendered_query,
    rich_controls, snapshot, wallet,
)


@pytest.mark.parametrize('kind', ['p', 'd', 'a', 'search'])
@pytest.mark.parametrize('count', [0, 1, 10, 11])
def test_customer_list_once_per_customer_and_pagination(kind, count):
    rows = [wallet(2000 + n, -(n + 1) if kind == 'd' else n + 1,
                   name=f'客户甲{n}', updated=str(n).zfill(3)) for n in range(count)]
    if kind == 'search':
        html, keyboard = jzbot.build_search_results_view('甲', rows[:10], count, 0)
    else:
        html, keyboard = jzbot.build_customer_list_view(rows, kind, 0)
    assert '<table' not in html
    customer_buttons = [b for b in buttons(rich_controls(html)) if (b.callback_data or '').startswith('user:')]
    assert len(customer_buttons) == min(10, count)
    assert len({b.callback_data for b in customer_buttons}) == len(customer_buttons)
    for button in customer_buttons:
        assert html.count('>' + button.text + '</tg-button>') == 1
        assert button.text.endswith('U')
    assert callbacks(rich_controls(html)) == callbacks(keyboard)
    next_page = 'search_page:1' if kind == 'search' else f'list:{kind}:1'
    assert (next_page in callbacks(keyboard)) == (count > 10)
    if count == 11:
        if kind == 'search':
            last, last_keyboard = jzbot.build_search_results_view('甲', rows[10:], count, 1)
        else:
            last, last_keyboard = jzbot.build_customer_list_view(rows, kind, 1)
        assert len([b for b in buttons(last_keyboard) if (b.callback_data or '').startswith('user:')]) == 1
        assert '<table' not in last and '第 2 / 2 页' in last
    check_view(html, keyboard)


def test_same_name_and_long_chinese_names_keep_distinct_customer_ids():
    for name in ['同名客户', '非常长的中文名字' * 40]:
        html, keyboard = jzbot.build_customer_list_view(
            [wallet(123456, 15, name=name), wallet(654321, 30, name=name)], 'p', 0,
        )
        entries = [b for b in buttons(keyboard) if (b.callback_data or '').startswith('user:')]
        assert {b.callback_data for b in entries} == {'user:123456:p:0', 'user:654321:p:0'}
        assert len(entries) == 2
        if len(name) > 22:
            assert all(len(b.text) < 50 for b in entries)
            assert '123456' in entries[1].text and '654321' in entries[0].text
        assert '<table' not in html
        check_view(html, keyboard)


@pytest.mark.parametrize('username', [None, 'valid_user', '<invalid>'])
async def test_detail_compact_operation_groups_without_duplicates(ledger_db, owner_id, customer_id, username):
    await add(ledger_db, owner_id, customer_id, username=username)
    detail = await ledger_db.get_customer_detail(owner_id, customer_id)
    html, keyboard = jzbot.build_customer_detail_view(detail, 's', 2)
    assert '<table bordered striped compact>' in html
    assert '<details><summary>最近 5 笔流水</summary>' in html and '<details open' not in html
    rows = rich_controls(html).inline_keyboard
    assert rows[0][0].copy_text and rows[0][1].callback_data == f'history:{customer_id}:0:s:2'
    assert [b.text for b in rows[0]] == ['复制摘要', '📒 全部流水']
    assert any(b.url for b in rows[1]) == (username == 'valid_user')
    assert rows[1][-1].callback_data == f'locate:{customer_id}:s:2'
    assert [b.text for b in rows[2]] == ['标记停用', '管理员归零']
    assert [b.callback_data for b in rows[2]] == [f'state:{customer_id}:1:s:2', f'zero:{customer_id}:s:2']
    assert rows[3][0].text == '🔄 刷新' and rows[3][0].callback_data == f'user:{customer_id}:s:2'
    assert [b.callback_data for b in rows[-1]] == ['search_page:2', 'home']
    assert html.index('</details>') < html.index('type="copy_text"')
    for b in buttons(keyboard):
        assert html.count('>' + b.text + '</tg-button>') == 1
    assert '摘要为页面快照' in html and '复制前请刷新' in html
    check_view(html, keyboard)


@pytest.mark.parametrize('kind', ['a', 'p', 'd', 's'])
async def test_refresh_reads_latest_statistics_recent_and_copy_without_writes(ledger_db, monkeypatch, owner_id, customer_id, kind):
    await add(ledger_db, owner_id, customer_id, 1, 150)
    first = await ledger_db.get_customer_detail(owner_id, customer_id)
    _, old_keyboard = jzbot.build_customer_detail_view(first, kind, 3)
    old_copy = next(b.copy_text.text for b in buttons(old_keyboard) if b.copy_text)
    assert '当前余额：15U' in old_copy
    await add(ledger_db, owner_id, customer_id, 2, -30)
    await add(ledger_db, owner_id, customer_id, 3, 5)
    before = await snapshot(ledger_db)
    monkeypatch.setattr(jzbot, 'db', ledger_db)
    context = SimpleNamespace(user_data={'customer_search_query': '张三'})
    update = callback_update(f'user:{customer_id}:{kind}:3', owner_id)
    await jzbot.on_admin_callback(update, context)
    rendered = rendered_query(update.callback_query)
    assert '<b>12.5 U</b>' in rendered['text']
    assert '+0.5｜余12.5U' in rendered['text']
    copied = next(b.copy_text.text for b in buttons(rendered['reply_markup']) if b.copy_text)
    assert '当前余额：12.5U' in copied and '累计入金：15.5U' in copied
    assert '累计出金：3U' in copied and '记账次数：3' in copied
    assert '当前余额：15U' in old_copy  # Native copy buttons are snapshots, not subscriptions.
    back = 'search_page:3' if kind == 's' else f'list:{kind}:3'
    assert back in callbacks(rendered['reply_markup'])
    assert context.user_data['customer_search_query'] == '张三'
    update.callback_query.answer.assert_awaited_once()
    bot = update.callback_query.get_bot()
    bot._post.assert_awaited_once()
    assert bot._post.await_args.args[0] == 'editMessageText'
    assert bot._post.await_args.kwargs['data']['reply_markup'] == {'inline_keyboard': []}
    assert await snapshot(ledger_db) == before


async def test_unchanged_refresh_ignores_not_modified_without_new_messages(ledger_db, monkeypatch, owner_id, customer_id):
    await add(ledger_db, owner_id, customer_id)
    monkeypatch.setattr(jzbot, 'db', ledger_db)
    update = callback_update(f'user:{customer_id}:a:0', owner_id)
    bot = update.callback_query.get_bot()
    bot._post.side_effect = BadRequest('Message is not modified')
    before = await snapshot(ledger_db)
    await jzbot.on_admin_callback(update, SimpleNamespace(user_data={}))
    update.callback_query.answer.assert_awaited_once()
    bot._post.assert_awaited_once()
    assert await snapshot(ledger_db) == before


async def test_list_button_opens_exact_same_named_customer_and_owner_isolation(ledger_db, monkeypatch, owner_id):
    for customer, amount in [(2001, 150), (2002, 30)]:
        await add(ledger_db, owner_id, customer, amount=amount, name='同名')
    await ledger_db.upsert_business_connection('other', owner_id + 1, True)
    await add(ledger_db, owner_id + 1, 2003, amount=999, name='OTHER OWNER')
    monkeypatch.setattr(jzbot, 'db', ledger_db)
    rows = await ledger_db.get_wallet_rows(owner_id)
    html, keyboard = jzbot.build_customer_list_view(rows, 'a', 0)
    assert 'OTHER OWNER' not in html
    entries = [b for b in buttons(keyboard) if (b.callback_data or '').startswith('user:')]
    assert len(entries) == 2
    for button in entries:
        update = callback_update(button.callback_data, owner_id)
        await jzbot.on_admin_callback(update, SimpleNamespace(user_data={}))
        copied = next(b.copy_text.text for b in buttons(rendered_query(update.callback_query)['reply_markup']) if b.copy_text)
        balance = '15U' if ':2001:' in button.callback_data else '3U'
        assert f'当前余额：{balance}' in copied
    before = await snapshot(ledger_db)
    forged = callback_update('user:2003:a:0', owner_id)
    await jzbot.on_admin_callback(forged, SimpleNamespace(user_data={}))
    assert 'OTHER OWNER' not in rendered_query(forged.callback_query)['text']
    assert not any(b.copy_text for b in buttons(rendered_query(forged.callback_query)['reply_markup']))
    assert await snapshot(ledger_db) == before


async def test_single_column_history_preserves_historical_values_clear_and_large_amount(ledger_db, owner_id, customer_id):
    await add(ledger_db, owner_id, customer_id, 1, 150)
    await add(ledger_db, owner_id, customer_id, 2, -30)
    await ledger_db.clear_balance(owner_id, customer_id, 3, 'business-1')
    large = 10 ** 100 + 5
    await add(ledger_db, owner_id, customer_id, 4, large)
    before = await snapshot(ledger_db)
    detail = await ledger_db.get_customer_detail(owner_id, customer_id)
    entries, total, page = await ledger_db.get_ledger_page(owner_id, customer_id)
    html, keyboard = jzbot.build_customer_history_view(detail, entries, total, page, 'p', 2)
    assert '<table' not in html and '<h2>' in html and '<hr/>' in html and '<footer>' in html
    assert '+15｜余15U' in html and '-3｜余12U' in html and '账本已清空' in html
    for entry in entries:
        timestamp, receipt = jzbot.admin_entry_line(entry).split('　', 1)
        assert f'<p>{timestamp}<br><b>{receipt}</b></p>' in html
    assert jzbot.format_tenths(large) in html
    assert f'user:{customer_id}:p:2' in callbacks(keyboard)
    assert await snapshot(ledger_db) == before
    check_view(html, keyboard)


async def test_list_inline_fallback_also_has_only_one_customer_display():
    bot = SimpleNamespace(_post=AsyncMock(side_effect=[BadRequest('Unsupported tg-button tag'), {'message_id': 1}]))
    html, keyboard = jzbot.build_customer_list_view([wallet(123, 320, name='唯一客户')], 'p', 0)
    await send_admin_rich(bot, 1001, html, keyboard)
    fallback = bot._post.await_args.kwargs['data']
    assert '唯一客户' not in fallback['rich_message']['html']
    customer_entries = [b for row in fallback['reply_markup']['inline_keyboard'] for b in row if b.get('callback_data', '').startswith('user:')]
    assert len(customer_entries) == 1 and customer_entries[0]['text'] == '唯一客户｜+32U'
    assert bot._post.await_count == 2
