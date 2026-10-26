from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, NetworkError, TimedOut

import compat_api as jzbot
from jzbot.infra.rich import (
    classify_rich_edit_error, edit_admin_rich, prepare_admin_page,
    rich_button_rows, send_admin_rich,
)
from test_admin_ui import (
    add, buttons, callback_update, callbacks, check_view, rich_controls, snapshot, wallet,
)


def test_home_native_compact_striped_table_heading_divider_footer_and_controls():
    html, keyboard = jzbot.build_home_view([wallet(balance=-1446)])
    assert '<h2>账本管理</h2>' in html
    assert '<table bordered striped compact>' in html
    assert '<th>账本概览</th><th>统计数值</th>' in html
    assert '<b>-144.6 U</b>' in html
    assert html.count('<hr/>') == 1 and html.endswith('</footer>')
    assert callbacks(rich_controls(html)) == callbacks(keyboard)
    payload = prepare_admin_page(html, keyboard)
    assert payload['rich_message']['html'] == html
    assert payload['reply_markup'] == {'inline_keyboard': []}
    check_view(html, keyboard)


@pytest.mark.parametrize('count', [0, 1, 6])
async def test_detail_native_details_closed_copy_and_clear(ledger_db, owner_id, customer_id, count):
    await add(ledger_db, owner_id, customer_id, 1, 150, name='张<&三>')
    if count == 0:
        await ledger_db.conn.execute('DELETE FROM ledger')  # Test DB only.
        await ledger_db.conn.commit()
    else:
        for index in range(count - 1):
            await add(ledger_db, owner_id, customer_id, index + 2, -5, name='张<&三>')
        await ledger_db.clear_balance(owner_id, customer_id, 100, 'business-1')
    detail = await ledger_db.get_customer_detail(owner_id, customer_id)
    html, keyboard = jzbot.build_customer_detail_view(detail, 's', 2)
    assert '<table bordered striped compact>' in html
    assert '<details><summary>最近 5 笔流水</summary>' in html
    assert '<details open' not in html
    disclosure = html.split('<details>', 1)[1].split('</details>', 1)[0]
    expected_lines = min(count + 1, 5) if count else 1
    assert disclosure.count('<p>') == expected_lines
    assert ('暂无流水记录' in disclosure) == (count == 0)
    if count:
        assert '账本已清空' in disclosure
    assert html.index('type="copy_text"') > html.index('</details>')
    assert html.index('data="history:') > html.index('</details>')
    assert 'search_page:2' in callbacks(rich_controls(html))
    copied = next(b.copy_text.text for b in buttons(keyboard) if b.copy_text)
    assert copied == jzbot.customer_summary_text(detail)
    assert '客户：张<&三>' in copied
    assert 'type="copy_text"' in html and '&lt;&amp;三&gt;' in html
    check_view(html, keyboard)


def summary_detail(name='张三', balance='320', deposit=1500, withdrawal=1180, count=25):
    return dict(wallet=wallet(balance=balance, name=name), deposit_total=deposit,
                withdraw_total=withdrawal, entry_count=count, recent=[])


def test_summary_exact_values_no_html_or_float_and_no_callback():
    detail = summary_detail()
    expected = '客户：张三\n当前余额：32U\n累计入金：150U\n累计出金：118U\n记账次数：25'
    assert jzbot.customer_summary_text(detail) == expected
    html, keyboard = jzbot.build_customer_detail_view(detail, 'p', 3)
    copy = next(b for b in buttons(keyboard) if b.copy_text)
    assert copy.copy_text.text == expected and copy.callback_data is None
    assert 'list:p:3' in callbacks(rich_controls(html))


@pytest.mark.parametrize('name', ['姓名' * 300, '😀<&>"' * 300])
def test_copy_limits_truncate_only_name_without_touching_amounts(name):
    detail = summary_detail(name=name, balance='-125', deposit=155, withdrawal=280)
    text = jzbot.customer_summary_text(detail)
    assert len(text.encode('utf-16-le')) // 2 <= 256
    assert '…\n当前余额：-12.5U\n累计入金：15.5U\n累计出金：28U\n记账次数：25' in text
    html, keyboard = jzbot.build_customer_detail_view(detail, 'a', 0)
    check_view(html, keyboard)


def test_unrepresentable_giant_amount_does_not_get_silently_truncated():
    detail = summary_detail(balance=str(10 ** 300))
    assert jzbot.customer_summary_text(detail) is None
    html, keyboard = jzbot.build_customer_detail_view(detail, 'a', 0)
    assert not any(b.copy_text for b in buttons(keyboard))
    assert '摘要超过复制长度限制' in html
    assert jzbot.format_tenths(10 ** 300) in html


def test_rich_button_attribute_escaping_and_limits():
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('<A&"B>', callback_data='x"<&')]])
    html = rich_button_rows(keyboard)
    parsed = buttons(rich_controls(html))[0]
    assert parsed.text == '<A&"B>' and parsed.callback_data == 'x"<&'
    with pytest.raises(ValueError, match='callback_data'):
        rich_button_rows(InlineKeyboardMarkup([[InlineKeyboardButton('x', callback_data='中' * 22)]]))
    with pytest.raises(ValueError, match='1-8'):
        rich_button_rows(InlineKeyboardMarkup([[InlineKeyboardButton(str(n), callback_data='x') for n in range(9)]]))


@pytest.mark.parametrize('method', ['send', 'edit'])
async def test_explicit_unsupported_body_button_falls_back_once_but_stays_rich(method, caplog):
    bot = SimpleNamespace(_post=AsyncMock(side_effect=[BadRequest('Unsupported tg-button tag'), {'message_id': 20}]))
    html, keyboard = jzbot.build_home_view([])
    if method == 'send':
        await send_admin_rich(bot, 1001, html, keyboard)
    else:
        query = SimpleNamespace(inline_message_id=None, message=SimpleNamespace(chat_id=1001, message_id=20), get_bot=lambda: bot)
        await edit_admin_rich(query, html, keyboard)
    assert bot._post.await_count == 2
    first, second = bot._post.await_args_list
    assert first.args[0] == second.args[0]
    fallback = second.kwargs['data']
    assert 'rich_message' in fallback and '<table bordered striped compact>' in fallback['rich_message']['html']
    assert '<tg-button' not in fallback['rich_message']['html']
    assert fallback['reply_markup'] == keyboard.to_dict()
    assert '正文按钮' in fallback['rich_message']['html']
    assert 'using inline controls' in caplog.text


async def test_rejected_fallback_is_bounded_not_infinite_or_new_messages():
    bot = SimpleNamespace(_post=AsyncMock(side_effect=BadRequest('Unsupported tg-button tag')))
    html, keyboard = jzbot.build_home_view([])
    with pytest.raises(BadRequest):
        await send_admin_rich(bot, 1001, html, keyboard)
    assert bot._post.await_count == 2


@pytest.mark.parametrize(('error', 'category'), [
    (BadRequest('message is not modified'), 'not_modified'),
    (BadRequest("message can't be edited"), 'uneditable'),
    (BadRequest('message to edit not found'), 'uneditable'),
    (BadRequest('Rich messages are not allowed'), 'rich_incompatible'),
    (TimedOut(), 'temporary_network'),
    (NetworkError('temporary'), 'temporary_network'),
    (BadRequest('random error'), 'api_error'),
])
async def test_edit_error_categories_do_not_send_new_messages(error, category, caplog):
    assert classify_rich_edit_error(error) == category
    query = callback_update('home', 1001).callback_query
    bot = query.get_bot()
    bot._post.side_effect = error
    html, keyboard = jzbot.build_home_view([])
    with pytest.raises(type(error)):
        await edit_admin_rich(query, html, keyboard)
    bot._post.assert_awaited_once()
    assert f'category={category}' in caplog.text


@pytest.mark.parametrize('callback', ['home', 'list:p:0', 'list:d:0', 'list:a:0', 'user:2001:a:0',
                                     'history:2001:1:a:0', 'search', 'search_page:0', 'search_cancel'])
async def test_body_actions_use_existing_handler_without_financial_changes(ledger_db, monkeypatch, owner_id, customer_id, callback):
    for index in range(11):
        await add(ledger_db, owner_id, customer_id, index + 1, 10)
    monkeypatch.setattr(jzbot, 'db', ledger_db)
    before = await snapshot(ledger_db)
    context = SimpleNamespace(user_data={'customer_search_query': '张'})
    update = callback_update(callback, owner_id)
    await jzbot.on_admin_callback(update, context)
    data = update.callback_query.get_bot()._post.await_args.kwargs['data']
    assert '<h2>' in data['rich_message']['html'] and '<footer>' in data['rich_message']['html']
    assert data['reply_markup'] == {'inline_keyboard': []}
    assert await snapshot(ledger_db) == before
    if callback == 'search_page:0':
        assert context.user_data['customer_search_query'] == '张'
    if callback == 'history:2001:1:a:0':
        assert '第 2 / 2 页' in data['rich_message']['html']
        assert 'user:2001:a:0' in callbacks(rich_controls(data['rich_message']['html']))


async def test_body_forged_customer_callbacks_cannot_read_other_owner(ledger_db, monkeypatch, owner_id, customer_id):
    await ledger_db.upsert_business_connection('other', owner_id + 1, True)
    await add(ledger_db, owner_id + 1, customer_id, name='PRIVATE OTHER OWNER')
    monkeypatch.setattr(jzbot, 'db', ledger_db)
    for action in ('user:2001:a:0', 'history:2001:0:a:0', 'locate:2001:a:0'):
        update = callback_update(action, owner_id)
        await jzbot.on_admin_callback(update, SimpleNamespace(user_data={}))
        if action.startswith('locate:'):
            assert '客户账本不存在' in update.callback_query.answer.await_args.args[0]
            update.callback_query.get_bot()._post.assert_not_awaited()
        else:
            data = update.callback_query.get_bot()._post.await_args.kwargs['data']
            assert 'PRIVATE OTHER OWNER' not in data['rich_message']['html']
