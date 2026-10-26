import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jzbot.runtime import Runtime
from test_v2_core import settings


def update(owner, customer, text='+15', source=1):
    message=SimpleNamespace(chat=SimpleNamespace(type='private',id=customer,
        username='fixture_user',first_name='合成',last_name='客户'),text=text,
        business_connection_id='business-1',sender_business_bot=None,
        from_user=SimpleNamespace(id=owner),message_id=source)
    return SimpleNamespace(business_message=message)


def context():
    return SimpleNamespace(bot_data={},user_data={},bot=SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=999)),
        pin_chat_message=AsyncMock(),get_business_connection=AsyncMock()))


@pytest.mark.parametrize('problem',['customer','self-bot','edited','group','disabled','invalid-text'])
async def test_business_guard_never_records_untrusted_update(ledger_db,owner_id,customer_id,problem):
    runtime=Runtime(settings(ledger_db.db_path),store=ledger_db);ctx=context()
    item=update(owner_id,customer_id)
    if problem=='customer': item.business_message.from_user.id=customer_id
    if problem=='self-bot': item.business_message.sender_business_bot=object()
    if problem=='edited': item.business_message=None
    if problem=='group': item.business_message.chat.type='group'
    if problem=='disabled': await ledger_db.mark_business_connection_disabled('business-1',owner_id)
    if problem=='invalid-text': item.business_message.text='+1.55'
    await runtime.business.on_business_message(item,ctx)
    assert await ledger_db.get_balance(owner_id,customer_id)==0
    assert (await (await ledger_db.conn.execute('SELECT COUNT(*) FROM processed_messages')).fetchone())[0]==0
    ctx.bot.send_message.assert_not_awaited()


async def test_commit_then_independent_worker_reply_and_pin(ledger_db,owner_id,customer_id):
    rt=Runtime(settings(ledger_db.db_path),store=ledger_db);ctx=context()
    await rt.business.on_business_message(update(owner_id,customer_id),ctx)
    assert await ledger_db.get_balance(owner_id,customer_id)==150
    ctx.bot.send_message.assert_not_awaited()
    assert rt.outbox.wakeup.is_set()
    await rt.outbox.process_due_outbox(ctx)
    sent=ctx.bot.send_message.await_args.kwargs
    assert sent['text']=='入金 15 USDT, 余额 15 USDT' and sent['reply_parameters'].message_id==1
    assert 'rich_message' not in sent and 'parse_mode' not in sent
    ctx.bot.pin_chat_message.assert_awaited_once()
    await rt.business.on_business_message(update(owner_id,customer_id),ctx)
    await rt.outbox.process_due_outbox(ctx)
    ctx.bot.send_message.assert_awaited_once()


async def test_background_worker_wakeup_and_shutdown_no_leaked_tasks(ledger_db,owner_id,customer_id):
    rt=Runtime(settings(ledger_db.db_path),store=ledger_db);ctx=context()
    ctx.bot_data['outbox_stop_event']=asyncio.Event()
    worker=asyncio.create_task(rt.outbox.outbox_worker(ctx))
    await asyncio.sleep(0)
    await rt.business.on_business_message(update(owner_id,customer_id),ctx)
    for _ in range(100):
        if ctx.bot.send_message.await_count: break
        await asyncio.sleep(.005)
    ctx.bot_data['outbox_stop_event'].set()
    await asyncio.wait_for(worker,1)
    ctx.bot.send_message.assert_awaited_once()


async def test_customer_recent_query_text_remains_plain_and_readonly(ledger_db,owner_id,customer_id):
    rt=Runtime(settings(ledger_db.db_path),store=ledger_db);ctx=context()
    await ledger_db.apply_amount(owner_id,customer_id,1,150,'business-1')
    await ledger_db.apply_amount(owner_id,customer_id,2,-30,'business-1')
    await rt.business.on_business_message(update(owner_id,customer_id,'/',3),ctx)
    text=ctx.bot.send_message.await_args.kwargs['text']
    assert '最近 2 条记录' in text and '+15 → 15' in text and '-3 → 12' in text
    assert '当前余额 12 USDT' in text
    assert (await (await ledger_db.conn.execute('SELECT COUNT(*) FROM ledger')).fetchone())[0]==2
