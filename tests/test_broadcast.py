"""Broadcast integration against isolated SQLite and Mock Telegram, never production."""

import asyncio
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

from jzbot.app import build_application
from jzbot.db.reconciliation import assert_preserved, snapshot
from jzbot.handlers.broadcast import BroadcastHandlers
from jzbot.repositories.broadcast import BroadcastRepository
from jzbot.runtime import Runtime
from jzbot.services.broadcast import BroadcastService, validate_body
from jzbot.ui.broadcast import audience_view, preview_view, progress_view
from test_v2_core import settings


def context(owner=1001):
    connection = SimpleNamespace(id='business-1', user=SimpleNamespace(id=owner),
                                 is_enabled=True, rights=SimpleNamespace(can_reply=True))
    return SimpleNamespace(user_data={}, bot_data={'broadcast_worker_task': Mock(done=Mock(return_value=False))},
        bot=SimpleNamespace(get_business_connection=AsyncMock(return_value=connection),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=801)),
            pin_chat_message=AsyncMock()))


def callback(owner, data):
    query = SimpleNamespace(data=data, message=SimpleNamespace(chat_id=owner),
                            inline_message_id=None, answer=AsyncMock())
    return query, SimpleNamespace(id=owner)


def text_update(owner, body):
    return SimpleNamespace(effective_user=SimpleNamespace(id=owner),
        effective_chat=SimpleNamespace(id=owner,type='private'),
        message=SimpleNamespace(text=body), business_message=None)


async def amount(store, owner, customer, delta, source=1):
    result = await store.apply_amount(owner,customer,source,delta,'business-1')
    await store.mark_outbox_delivered(result['outbox_id'])
    return result


@pytest_asyncio.fixture
async def broadcast(ledger_db, tmp_path):
    service = BroadcastService(ledger_db, tmp_path/'broadcast.db')
    await service.queue.open()
    try:
        yield service
    finally:
        await service.queue.close()


async def campaign(broadcast, owner=1001, customer=2001):
    await amount(broadcast.store,owner,customer,10)
    request=await broadcast.prepare(owner,['all'],'客户通知\n测试正文')
    job=await broadcast.confirm(owner,request)
    return request,job


async def test_audience_zero_deposit_negative_debt_union_and_owner_isolation(broadcast,owner_id,customer_id):
    store=broadcast.store
    await amount(store,owner_id,customer_id,150)
    await amount(store,owner_id,customer_id+1,-50)
    await amount(store,owner_id,customer_id+2,10)
    result=await store.clear_balance(owner_id,customer_id+2,2,'business-1')
    await store.mark_outbox_delivered(result['outbox_id'])
    await store.upsert_customer_profile(owner_id,customer_id+3,None,'仅资料','无交易')
    await store.upsert_business_connection('other',owner_id+1,True)
    await amount(store,owner_id+1,customer_id+4,20)
    ids=lambda rows:[r['customer_chat_id'] for r in rows]
    assert ids(await broadcast.recipients(owner_id,['deposit']))==[customer_id,customer_id+2]
    assert ids(await broadcast.recipients(owner_id,['debt']))==[customer_id+1]
    assert ids(await broadcast.recipients(owner_id,['all']))==[customer_id,customer_id+1,customer_id+2]
    assert ids(await broadcast.recipients(owner_id,['all','deposit','debt']))==[customer_id,customer_id+1,customer_id+2]
    assert ids(await broadcast.recipients(owner_id+1,['all']))==[customer_id+4]


async def test_huge_integer_sign_is_exact_without_real_cast(broadcast,owner_id,customer_id):
    await amount(broadcast.store,owner_id,customer_id,10**200+1)
    await amount(broadcast.store,owner_id,customer_id+1,-(10**200+1))
    assert (await broadcast.recipients(owner_id,['deposit']))[0]['customer_chat_id']==customer_id
    assert (await broadcast.recipients(owner_id,['debt']))[0]['customer_chat_id']==customer_id+1


@pytest.mark.parametrize('selection',[[],['invalid'],['all','forged']])
async def test_invalid_selection_rejected(broadcast,selection):
    with pytest.raises(ValueError):
        await broadcast.recipients(1001,selection)


@pytest.mark.parametrize('body',['','   ','\n','x'*2001,'😀'*1001,'x\x00y'])
def test_invalid_or_too_long_text(body):
    with pytest.raises(ValueError):validate_body(body)


def test_unicode_boundary_and_plain_content():
    assert validate_body('😀'*1000)=='😀'*1000
    assert validate_body('<b>原样</b>\n通知')=='<b>原样</b>\n通知'


async def test_preview_does_not_queue_or_send_before_confirmation(broadcast,owner_id,customer_id):
    await amount(broadcast.store,owner_id,customer_id,10)
    request=await broadcast.prepare(owner_id,['all'],'只预览')
    assert request['body']=='只预览'
    assert (await broadcast.queue.list_jobs(owner_id))[1]==0
    assert await broadcast.queue.claim(time.time()) is None


async def test_confirmation_idempotency_and_frozen_recipient_content(broadcast):
    request,job=await campaign(broadcast)
    assert await broadcast.confirm(1001,request)==job
    saved=await broadcast.queue.get(1001,job)
    assert saved['total']==1 and saved['body']==request['body']
    assert (await broadcast.queue.list_jobs(1001))[1]==1


async def test_expired_confirmation_and_audience_changes_rejected(broadcast,owner_id,customer_id):
    await amount(broadcast.store,owner_id,customer_id,10)
    request=await broadcast.prepare(owner_id,['deposit'],'预览',now=100)
    with pytest.raises(ValueError,match='过期'):
        await broadcast.confirm(owner_id,request,now=400)
    request=await broadcast.prepare(owner_id,['deposit'],'预览')
    await amount(broadcast.store,owner_id,customer_id,-20,source=2)
    with pytest.raises(ValueError,match='范围已变化'):
        await broadcast.confirm(owner_id,request)


async def test_second_active_job_is_rejected(broadcast):
    await campaign(broadcast)
    request=await broadcast.prepare(1001,['all'],'第二条')
    with pytest.raises(sqlite3.IntegrityError):
        await broadcast.confirm(1001,request)
    assert (await broadcast.queue.list_jobs(1001))[1]==1


async def test_owner_cannot_confirm_read_control_others_job(broadcast):
    request,job=await campaign(broadcast)
    with pytest.raises(PermissionError):
        await broadcast.confirm(1002,request)
    assert await broadcast.queue.get(1002,job) is None
    assert await broadcast.queue.recipient_page(1002,job,0)==([],0)
    assert not await broadcast.queue.control(1002,job,'cancel')


async def test_send_plain_silent_not_reply_or_pin_and_finances_unchanged(broadcast):
    request,job=await campaign(broadcast)
    before=snapshot(broadcast.store.db_path)
    ctx=context()
    assert await broadcast.process_one(ctx)
    ctx.bot.send_message.assert_awaited_once_with(chat_id=2001,business_connection_id='business-1',
                                                text=request['body'],disable_notification=True)
    ctx.bot.pin_chat_message.assert_not_awaited()
    assert (await broadcast.queue.get(1001,job))['counts']=={'sent':1}
    assert_preserved(before,snapshot(broadcast.store.db_path))
    assert not await broadcast.process_one(ctx)


async def test_financial_receipts_have_priority(broadcast):
    _,job=await campaign(broadcast)
    new=await broadcast.store.apply_amount(1001,2001,2,5,'business-1')
    ctx=context()
    assert not await broadcast.process_one(ctx)
    ctx.bot.send_message.assert_not_awaited()
    await broadcast.store.mark_outbox_delivered(new['outbox_id'])
    assert await broadcast.process_one(ctx)
    assert (await broadcast.queue.get(1001,job))['status']=='completed'


@pytest.mark.parametrize('reason',['disabled','permission','owner-mismatch'])
async def test_bad_live_connection_never_sends(broadcast,reason):
    _,job=await campaign(broadcast)
    ctx=context();c=ctx.bot.get_business_connection.return_value
    if reason=='disabled':c.is_enabled=False
    if reason=='permission':c.rights.can_reply=False
    if reason=='owner-mismatch':c.user.id=1002
    before=snapshot(broadcast.store.db_path)
    assert await broadcast.process_one(ctx)
    ctx.bot.send_message.assert_not_awaited()
    assert (await broadcast.queue.get(1001,job))['counts']=={'failed':1}
    assert_preserved(before,snapshot(broadcast.store.db_path))


async def test_first_invalid_connection_falls_back_to_second(broadcast):
    _,job=await campaign(broadcast)
    await broadcast.store.upsert_business_connection('business-2',1001,True)
    ctx=context()
    good=ctx.bot.get_business_connection.return_value
    calls=[]
    async def refreshed(cid):
        calls.append(cid)
        if len(calls)==1:raise BadRequest('BUSINESS_CONNECTION_INVALID')
        return SimpleNamespace(id=cid,user=good.user,is_enabled=True,rights=good.rights)
    ctx.bot.get_business_connection.side_effect=refreshed
    await broadcast.process_one(ctx)
    assert len(calls)==2
    assert (await broadcast.queue.get(1001,job))['counts']=={'sent':1}


@pytest.mark.parametrize('exc,expected',[ (TimedOut(),'uncertain'),(NetworkError('network down'),'uncertain'),
    (BadRequest('business_peer_usage_missing'),'failed'),(Forbidden('bot was blocked by the user'),'failed')])
async def test_send_errors_classified_and_never_reaccounted(broadcast,exc,expected):
    _,job=await campaign(broadcast)
    before=snapshot(broadcast.store.db_path);ctx=context()
    ctx.bot.send_message.side_effect=exc
    await broadcast.process_one(ctx)
    assert (await broadcast.queue.get(1001,job))['counts']=={expected:1}
    assert not await broadcast.process_one(ctx,now=time.time()+1000)
    ctx.bot.send_message.assert_awaited_once()
    assert_preserved(before,snapshot(broadcast.store.db_path))


async def test_get_connection_network_retry_bounded_without_disable(broadcast):
    _,job=await campaign(broadcast);ctx=context()
    ctx.bot.get_business_connection.side_effect=TimedOut()
    before=snapshot(broadcast.store.db_path)
    start=time.time()
    for n in range(3):assert await broadcast.process_one(ctx,now=start+31*n)
    assert (await broadcast.queue.get(1001,job))['counts']=={'failed':1}
    ctx.bot.send_message.assert_not_awaited()
    assert_preserved(before,snapshot(broadcast.store.db_path))


async def test_retry_after_blocks_queue_and_sent_recipient_never_replayed(broadcast):
    _,job=await campaign(broadcast);ctx=context()
    ctx.bot.send_message.side_effect=RetryAfter(30)
    await broadcast.process_one(ctx)
    assert not await broadcast.process_one(ctx,now=time.time()+5)
    ctx.bot.send_message.side_effect=None
    assert await broadcast.process_one(ctx,now=time.time()+31)
    assert (await broadcast.queue.get(1001,job))['counts']=={'sent':1}
    assert not await broadcast.process_one(ctx,now=time.time()+1000)
    assert ctx.bot.send_message.await_count==2


async def test_pause_resume_cancel_and_other_jobs_not_affected(broadcast):
    _,job=await campaign(broadcast);ctx=context()
    assert await broadcast.queue.control(1001,job,'pause')
    assert not await broadcast.process_one(ctx)
    assert await broadcast.queue.control(1001,job,'resume')
    assert await broadcast.queue.control(1001,job,'cancel')
    assert not await broadcast.process_one(ctx)
    assert (await broadcast.queue.get(1001,job))['counts']=={'cancelled':1}


async def test_cancel_while_refreshing_does_not_send(broadcast):
    _,job=await campaign(broadcast);ctx=context();original=ctx.bot.get_business_connection.return_value
    async def refresh(cid):
        await broadcast.queue.control(1001,job,'cancel')
        return original
    ctx.bot.get_business_connection.side_effect=refresh
    await broadcast.process_one(ctx)
    ctx.bot.send_message.assert_not_awaited()
    assert (await broadcast.queue.get(1001,job))['counts']=={'cancelled':1}


async def test_acknowledgement_failure_stops_worker_instead_of_sending_others(broadcast,monkeypatch):
    _,job=await campaign(broadcast);ctx=context()
    monkeypatch.setattr(broadcast.queue,'finish',AsyncMock(side_effect=RuntimeError('disk fixture')))
    with pytest.raises(RuntimeError):await broadcast.process_one(ctx)
    assert (await broadcast.queue.get(1001,job))['counts']=={'sending':1}
    assert not await broadcast.process_one(ctx)
    ctx.bot.send_message.assert_awaited_once()


async def test_restart_pauses_pending_and_marks_inflight_uncertain(broadcast):
    _,job=await campaign(broadcast)
    row=await broadcast.queue.claim(time.time())
    assert row
    await broadcast.queue.close();await broadcast.queue.open()
    saved=await broadcast.queue.get(1001,job)
    assert saved['status']=='paused' and saved['counts']=={'uncertain':1}
    ctx=context()
    assert not await broadcast.process_one(ctx)
    assert await broadcast.queue.control(1001,job,'resume')
    assert not await broadcast.process_one(ctx)
    assert (await broadcast.queue.get(1001,job))['status']=='completed'
    ctx.bot.send_message.assert_not_awaited()


@pytest.mark.parametrize('total',[0,1,10,11,100])
async def test_results_page_size_order_and_isolation(broadcast,total):
    rows=[dict(customer_chat_id=2000+n,name=f'客户{n}') for n in range(total)]
    if not total:
        assert (await broadcast.queue.list_jobs(1001))[1]==0
        return
    await broadcast.queue.create('test-job',1001,'正文','all',rows,time.time())
    seen=[]
    for page in range((total+9)//10):
        result,count=await broadcast.queue.recipient_page(1001,'test-job',page)
        assert count==total and len(result)<=10
        seen.extend(r['customer'] for r in result)
    assert seen==[r['customer_chat_id'] for r in rows]
    assert await broadcast.queue.recipient_page(1002,'test-job',0)==([],0)


async def test_refuse_financial_database_and_separate_path(broadcast,tmp_path):
    before=snapshot(broadcast.store.db_path)
    with pytest.raises(ValueError):BroadcastService(broadcast.store,broadcast.store.db_path)
    repo=BroadcastRepository(broadcast.store.db_path)
    with pytest.raises(ValueError,match='Unexpected tables'):await repo.open()
    assert repo.conn is None
    assert_preserved(before,snapshot(broadcast.store.db_path))


async def test_form_flow_preview_confirm_duplicate_and_cancel(broadcast,monkeypatch):
    await amount(broadcast.store,1001,2001,10)
    handler=BroadcastHandlers(broadcast);ctx=context()
    edit=AsyncMock();send=AsyncMock()
    monkeypatch.setattr('jzbot.handlers.broadcast.safe_edit_query',edit)
    monkeypatch.setattr('jzbot.handlers.broadcast.send_admin_rich',send)
    q,user=callback(1001,'bc:new');assert await handler.on_callback(q,user,ctx,q.data)
    q.data='bc:toggle:all';await handler.on_callback(q,user,ctx,q.data)
    q.data='bc:compose';await handler.on_callback(q,user,ctx,q.data)
    assert ctx.user_data['awaiting_broadcast_text']
    assert await handler.on_text(text_update(1001,'通知 <测试>'),ctx)
    preview=ctx.user_data['broadcast_preview']
    assert '&lt;测试&gt;' in send.await_args.args[2]
    q.data='bc:confirm:'+preview['id'];await handler.on_callback(q,user,ctx,q.data)
    await handler.on_callback(q,user,ctx,q.data)
    assert (await broadcast.queue.list_jobs(1001))[1]==1
    assert ctx.user_data.get('broadcast_preview') is None
    q.data='bc:cancel';await handler.on_callback(q,user,ctx,q.data)
    ctx.bot.send_message.assert_not_awaited()


async def test_callback_forgery_and_business_messages_do_not_enter_form(broadcast,monkeypatch):
    handler=BroadcastHandlers(broadcast);ctx=context()
    ctx.user_data.update(awaiting_broadcast_text=True,broadcast_selected=['all'],broadcast_input_expires=time.time()+100)
    q,user=callback(1002,'bc:new')
    assert await handler.on_callback(q,user,ctx,q.data)
    q.answer.assert_awaited_once_with('无权操作此广播。',show_alert=True)
    assert (await broadcast.queue.list_jobs(1002))[1]==0
    update=text_update(1001,'内容');update.message=None
    assert await handler.on_text(update,ctx)
    assert 'broadcast_preview' not in ctx.user_data


async def test_search_and_broadcast_states_do_not_overlap(ledger_db,monkeypatch):
    rt=Runtime(settings(ledger_db.db_path),store=ledger_db);ctx=context()
    ctx.user_data.update(awaiting_broadcast_text=True,broadcast_preview={'id':'old'})
    query,user=callback(1001,'search')
    monkeypatch.setattr('jzbot.handlers.admin.safe_edit_query',AsyncMock())
    await rt.admin.on_admin_callback(SimpleNamespace(callback_query=query,effective_user=user),ctx)
    assert ctx.user_data['awaiting_customer_search']
    assert 'awaiting_broadcast_text' not in ctx.user_data and 'broadcast_preview' not in ctx.user_data


def test_native_rich_preview_escaping_buttons_and_zero_label():
    request=dict(id='a'*24,selected=['deposit'],body='通知 <b> &\n下一行',
                 recipients=[dict(customer_chat_id=1,name='<客户>')])
    text,keyboard=preview_view(request)
    assert '<table bordered striped compact>' in text and '&lt;b&gt;' in text and '&lt;客户&gt;' in text
    assert '余额 ≥ 0' in text and '<tg-button' in text
    for row in keyboard.inline_keyboard:
        for button in row:assert len(button.callback_data.encode())<=64
    text,_=audience_view(['deposit'],dict(all=3,deposit=2,debt=1))
    assert '余额为 0 归入存款用户' in text


async def test_worker_lifecycle_with_application(tmp_path):
    conf=settings(tmp_path/'lifecycle.db');rt=Runtime(conf)
    app=build_application(conf,runtime=rt)
    fake=SimpleNamespace(bot_data={},bot=SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=1,can_connect_to_business=True)),
        set_my_commands=AsyncMock()))
    await app.post_init(fake)
    task=fake.bot_data['broadcast_worker_task']
    await asyncio.sleep(0)
    assert rt.broadcast.queue.conn is not None and not task.done()
    await app.post_shutdown(fake)
    assert task.done() and rt.broadcast.queue.conn is None and rt.store.conn is None


async def test_concurrent_delivery_claim_sends_only_once(broadcast):
    _,job=await campaign(broadcast);ctx=context()
    assert await asyncio.gather(broadcast.process_one(ctx),broadcast.process_one(ctx))==[True,False]
    ctx.bot.send_message.assert_awaited_once()
    assert (await broadcast.queue.get(1001,job))['counts']=={'sent':1}


async def test_cancel_worker_during_request_quarantines_recipient(broadcast):
    _,job=await campaign(broadcast);ctx=context();entered=asyncio.Event()
    async def send(**kwargs):
        entered.set()
        await asyncio.Event().wait()
    ctx.bot.send_message.side_effect=send
    task=asyncio.create_task(broadcast.process_one(ctx))
    await entered.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert (await broadcast.queue.get(1001,job))['counts']=={'uncertain':1}
    assert not await broadcast.process_one(ctx)


async def test_recipient_limit_stops_preview_without_creating_job(broadcast,monkeypatch):
    for n in range(3):await amount(broadcast.store,1001,2001+n,10)
    monkeypatch.setattr('jzbot.services.broadcast.MAX_RECIPIENTS',2)
    with pytest.raises(ValueError,match='最多 2 人'):
        await broadcast.prepare(1001,['all'],'不应发送')
    assert (await broadcast.queue.list_jobs(1001))[1]==0


async def test_empty_audience_stops_preview(broadcast):
    with pytest.raises(ValueError,match='没有客户'):
        await broadcast.prepare(1001,['all'],'不应发送')
    assert (await broadcast.queue.list_jobs(1001))[1]==0


async def test_failed_ack_stops_worker_and_does_not_block_financial_shutdown(tmp_path,monkeypatch):
    conf=settings(tmp_path/'shutdown.db');rt=Runtime(conf);app=build_application(conf,runtime=rt)
    fake=SimpleNamespace(bot_data={},bot=SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=1,can_connect_to_business=True)),
        set_my_commands=AsyncMock()))
    await app.post_init(fake)
    original=rt.broadcast.queue.close
    async def close_failure():
        await original()
        raise RuntimeError('fixture close failure')
    monkeypatch.setattr(rt.broadcast.queue,'close',close_failure)
    await app.post_shutdown(fake)
    assert rt.store.conn is None and fake.bot_data['outbox_worker_task'].done()
