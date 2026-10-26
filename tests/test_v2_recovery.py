import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, TimedOut

from jzbot.services.recovery import ReceiptRecoveryService
from jzbot.runtime import Runtime
from test_v2_core import settings
from test_admin_ui import callback_update, rendered_query, callbacks, rich_controls


def context(owner=1001, *, enabled=True, reply=True, connection_id='business-1'):
    conn=SimpleNamespace(id=connection_id,user=SimpleNamespace(id=owner),is_enabled=enabled,
                         rights=SimpleNamespace(can_reply=reply))
    return SimpleNamespace(user_data={},bot=SimpleNamespace(
        get_business_connection=AsyncMock(return_value=conn),
        send_message=AsyncMock(side_effect=AssertionError('recovery must not send')),
        pin_chat_message=AsyncMock(side_effect=AssertionError('recovery must not pin'))))


async def finance(store):
    return {table:[tuple(r) for r in await (await store.conn.execute('SELECT * FROM '+table)).fetchall()]
            for table in ('wallets','ledger','wallet_stats','processed_messages','admin_operations','customer_management')}


async def create(store, owner,customer):
    result=await store.apply_amount(owner,customer,1,150,'business-1')
    return result['outbox_id']


async def test_all_pending_view_does_not_label_fresh_as_error(ledger_db,owner_id,customer_id):
    outbox=await create(ledger_db,owner_id,customer_id)
    assert await ledger_db.get_outbox_attention_count(owner_id)==0
    rows,total,page=await ledger_db.get_pending_receipts_page(owner_id)
    assert len(rows)==total==1 and rows[0]['id']==outbox
    rt=Runtime(settings(ledger_db.db_path),store=ledger_db)
    update=callback_update('receipts:0',owner_id)
    await rt.admin.on_admin_callback(update,context())
    html=rendered_query(update.callback_query)['text']
    assert '等待任务不等于故障' in html and 'receipt_check:' in html


async def test_recovery_confirm_keeps_money_schedule_state_and_never_sends(ledger_db,owner_id,customer_id):
    outbox=await create(ledger_db,owner_id,customer_id)
    await ledger_db.mark_outbox_failed(outbox,TimedOut())
    before=dict(await ledger_db.get_outbox(outbox)); money=await finance(ledger_db)
    service=ReceiptRecoveryService(ledger_db); ctx=context()
    req=await service.prepare(ctx,owner_id,outbox)
    assert req is not None
    assert await service.confirm(ctx,owner_id,req)=='checked'
    assert dict(await ledger_db.get_outbox(outbox))==before
    assert await finance(ledger_db)==money
    ctx.bot.send_message.assert_not_awaited();ctx.bot.pin_chat_message.assert_not_awaited()


async def test_invalid_connection_falls_back_then_rebinds_only_after_confirmation(ledger_db,owner_id,customer_id):
    outbox=await create(ledger_db,owner_id,customer_id)
    await ledger_db.upsert_business_connection('new-connection',owner_id,True)
    ctx=context(connection_id='new-connection')
    ctx.bot.get_business_connection.side_effect=[BadRequest('BUSINESS_CONNECTION_INVALID'),
        ctx.bot.get_business_connection.return_value,ctx.bot.get_business_connection.return_value]
    service=ReceiptRecoveryService(ledger_db); before=dict(await ledger_db.get_outbox(outbox))
    req=await service.prepare(ctx,owner_id,outbox)
    assert req['connection_id']=='new-connection'
    assert dict(await ledger_db.get_outbox(outbox))==before
    assert await service.confirm(ctx,owner_id,req)=='rebound'
    after=dict(await ledger_db.get_outbox(outbox));before['business_connection_id']='new-connection'
    assert after==before


@pytest.mark.parametrize('problem',['expired','delivered','attempt-changed','disabled','cannot-reply'])
async def test_stale_or_unauthorized_recovery_never_force_sends(ledger_db,owner_id,customer_id,problem):
    outbox=await create(ledger_db,owner_id,customer_id)
    ctx=context();service=ReceiptRecoveryService(ledger_db)
    req=await service.prepare(ctx,owner_id,outbox)
    if problem=='expired': req['expires']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
    if problem=='delivered': await ledger_db.mark_outbox_delivered(outbox)
    if problem=='attempt-changed': await ledger_db.mark_outbox_failed(outbox,TimedOut())
    if problem=='disabled': ctx.bot.get_business_connection.return_value.is_enabled=False
    if problem=='cannot-reply': ctx.bot.get_business_connection.return_value.rights.can_reply=False
    before=dict(await ledger_db.get_outbox(outbox))
    assert await service.confirm(ctx,owner_id,req) in {'expired','stale','unavailable'}
    assert dict(await ledger_db.get_outbox(outbox))==before
    ctx.bot.send_message.assert_not_awaited()


async def test_recovery_owner_isolation(ledger_db,owner_id,customer_id):
    outbox=await create(ledger_db,owner_id,customer_id)
    await ledger_db.upsert_business_connection('b',owner_id+1,True)
    service=ReceiptRecoveryService(ledger_db)
    assert await service.prepare(context(owner_id+1),owner_id+1,outbox) is None
    req=await service.prepare(context(),owner_id,outbox)
    with pytest.raises(PermissionError): await service.confirm(context(),owner_id+1,req)
    with pytest.raises(PermissionError): await service.prepare(context(owner_id+1),owner_id,outbox)


async def test_recovery_timeout_does_not_disable(ledger_db,owner_id,customer_id):
    outbox=await create(ledger_db,owner_id,customer_id)
    ctx=context();ctx.bot.get_business_connection.side_effect=TimedOut()
    assert await ReceiptRecoveryService(ledger_db).prepare(ctx,owner_id,outbox) is None
    assert await ledger_db.get_connection_owner('business-1')==(owner_id,True)


async def test_recovery_cancel_and_repeat_are_readonly(ledger_db,owner_id,customer_id):
    outbox=await create(ledger_db,owner_id,customer_id)
    runtime=Runtime(settings(ledger_db.db_path),store=ledger_db);ctx=context()
    before=dict(await ledger_db.get_outbox(outbox))
    await runtime.admin.on_admin_callback(callback_update(f'receipt_check:{outbox}:0',owner_id),ctx)
    nonce=next(iter(ctx.user_data['receipt_recovery_requests']))
    for _ in range(2):
        await runtime.admin.on_admin_callback(callback_update('receipt_cancel:'+nonce,owner_id),ctx)
    assert dict(await ledger_db.get_outbox(outbox))==before
    ctx.bot.send_message.assert_not_awaited()


async def test_deposit_source_navigation_and_admin_zero_preserve_old_constraint(ledger_db,owner_id,customer_id):
    await create(ledger_db,owner_id,customer_id)
    runtime=Runtime(settings(ledger_db.db_path),store=ledger_db);ctx=context()
    for action in (f'list:i:0',f'user:{customer_id}:i:0',f'history:{customer_id}:0:i:0'):
        update=callback_update(action,owner_id)
        await runtime.admin.on_admin_callback(update,ctx)
        html=rendered_query(update.callback_query)['text']
        assert '<h2>' in html and '<footer>' in html
    await runtime.admin.on_admin_callback(callback_update(f'zero:{customer_id}:i:0',owner_id),ctx)
    nonce=next(iter(ctx.user_data['admin_zero_requests']))
    req=ctx.user_data['admin_zero_requests'][nonce]
    assert req['back_kind']=='a' and req['ui_back_kind']=='i'
    update=callback_update('zero_confirm:'+nonce,owner_id)
    await runtime.admin.on_admin_callback(update,ctx)
    assert 'list:i:0' in callbacks(rich_controls(rendered_query(update.callback_query)['text']))
    assert await ledger_db.get_balance(owner_id,customer_id)==0
