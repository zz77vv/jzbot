"""Owner-scoped audience previews and a conservative independent sender."""

import asyncio
import logging
import secrets
import time
from datetime import timedelta

from telegram.error import (
    BadRequest,
    Forbidden,
    NetworkError,
    RetryAfter,
    TelegramError,
)

from jzbot.domain import (
    classify_locate_error,
    customer_display_name,
)
from jzbot.repositories.broadcast import BroadcastRepository

logger = logging.getLogger('jzbot')
AUDIENCES = {'all': '有交易记录的客户', 'deposit': '存款用户（余额 ≥ 0）', 'debt': '欠款用户（余额 < 0）'}
MAX_RECIPIENTS = 1000
MAX_TEXT_UNITS = 2000
PREVIEW_TTL = 300


def validate_body(body):
    if not isinstance(body, str) or not body.strip():
        raise ValueError('广播内容不能为空。')
    if len(body.encode('utf-16-le')) // 2 > MAX_TEXT_UNITS:
        raise ValueError(f'广播内容最多 {MAX_TEXT_UNITS} 个字符单位，请缩短后重试。')
    if any(ord(c) < 32 and c not in '\n\r\t' for c in body):
        raise ValueError('广播内容含不支持的控制字符。')
    return body


class BroadcastService:
    def __init__(self, store, path):
        self.store = store
        if path.resolve() == store.db_path.resolve():
            raise ValueError('Broadcast queue must be separate from the ledger')
        self.queue = BroadcastRepository(path)
        self.send_lock = asyncio.Lock()
        self.wakeup = asyncio.Event()

    async def recipients(self, owner, selected):
        if not selected or not set(selected) <= AUDIENCES.keys():
            raise ValueError('请至少选择一种有效客户范围。')
        async with self.store.write_lock:
            rows = await self.store.get_broadcast_customer_rows(owner, selected, MAX_RECIPIENTS + 1)
        if len(rows) > MAX_RECIPIENTS:
            raise ValueError(f'单次广播最多 {MAX_RECIPIENTS} 人，请缩小范围。')
        # Wallet PK makes this a union without duplicates, even for multi-select.
        return [{'customer_chat_id': int(r['customer_chat_id']),
                 'name': customer_display_name(r)[:80]} for r in rows]

    async def prepare(self, owner, selected, body, *, now=None):
        if not await self.store.is_business_owner(owner):
            raise PermissionError('无权创建广播')
        body = validate_body(body)
        recipients = await self.recipients(owner, selected)
        if not recipients:
            raise ValueError('当前范围没有客户，请返回重新选择。')
        now = time.time() if now is None else now
        return dict(id=secrets.token_hex(12), owner=owner, selected=sorted(selected),
                    body=body, recipients=recipients, expires=now + PREVIEW_TTL)

    async def confirm(self, owner, request, *, now=None):
        now = time.time() if now is None else now
        if request['owner'] != owner or not await self.store.is_business_owner(owner):
            raise PermissionError('无权发送此广播')
        if now >= request['expires']:
            raise ValueError('预览已过期，请重新生成后确认。')
        # A balance or audience change invalidates the preview instead of silently
        # replacing its recipients. Names are a display-only snapshot.
        current = await self.recipients(owner, request['selected'])
        if [r['customer_chat_id'] for r in current] != [r['customer_chat_id'] for r in request['recipients']]:
            raise ValueError('客户范围已变化，请重新生成预览后确认。')
        validate_body(request['body'])
        await self.queue.create(request['id'], owner, request['body'],
                                ','.join(request['selected']), request['recipients'], now)
        self.wakeup.set()
        return request['id']

    async def _send(self, context, row):
        failures = []
        ids = (await self.store.get_enabled_connection_ids(row['owner']))[:8]
        for cid in ids:
            try:
                c = await context.bot.get_business_connection(cid)
                if c.id != cid or int(c.user.id) != row['owner']:
                    failures.append('connection_invalid')
                    continue
                if not c.is_enabled:
                    failures.append('connection_disabled')
                    continue
                rights = getattr(c, 'rights', None)
                if getattr(rights, 'can_reply', None) is False or (rights is None and getattr(c, 'can_reply', True) is False):
                    failures.append('no_reply_permission')
                    continue
            except RetryAfter:
                raise
            except TelegramError as exc:
                failures.append(classify_locate_error(exc).value)
                continue
            # No ledger writes, dots, replies, pins or paid broadcasts.
            current = await self.queue.get(row['owner'], row['job_id'])
            if current is None or current['status'] != 'running':
                return ('cancelled' if current and current['status'] == 'cancelled' else 'pending',
                        None, None, None)
            try:
                sent = await context.bot.send_message(
                    chat_id=row['customer'], business_connection_id=cid,
                    text=row['body'], disable_notification=True,
                )
                return ('sent', None, cid, sent.message_id)
            except RetryAfter:
                raise
            except NetworkError as exc:
                if not isinstance(exc, BadRequest):
                    return ('uncertain', 'send_uncertain', cid, None)
                category = classify_locate_error(exc).value
                if category in ('connection_invalid', 'connection_disabled', 'no_reply_permission'):
                    failures.append(category)
                    continue
                return ('failed', category, cid, None)
            except TelegramError as exc:
                category = classify_locate_error(exc).value
                if category in ('connection_invalid', 'connection_disabled', 'no_reply_permission'):
                    failures.append(category)
                    continue
                return ('uncertain' if category == 'unknown' and not isinstance(exc, Forbidden) else 'failed', category, cid, None)
        # Refresh failures are safe to retry: no send was attempted on them.
        for category in ('temporary', 'no_reply_permission', 'connection_disabled', 'connection_invalid'):
            if category in failures:
                return ('pending' if category == 'temporary' else 'failed', category, None, None)
        return ('failed', 'no_connection', None, None)

    async def process_one(self, context, *, now=None):
        now = time.time() if now is None else now
        async with self.send_lock:
            # Give normal financial receipts priority; never hold their send lock.
            async with self.store.write_lock:
                if await self.store.get_due_outbox(limit=1):
                    return False
            row = await self.queue.claim(now)
            if row is None:
                return False
            try:
                status, category, cid, message = await self._send(context, row)
                if status == 'pending' and row['attempts'] >= 3:
                    status = 'failed'
                await self.queue.finish(row, status, category, due=now + 30,
                                        connection=cid, message_id=message)
            except RetryAfter as exc:
                delay = exc.retry_after
                delay = delay.total_seconds() if isinstance(delay, timedelta) else int(delay)
                deadline = time.time() + max(1, delay)
                await self.queue.finish(row, 'pending' if row['attempts'] < 3 else 'failed',
                                        'rate_limit', due=deadline, cooldown=deadline)
                status, category, cid = 'deferred', 'rate_limit', None
            except asyncio.CancelledError:
                # Cancellation during a send can be ambiguous; restart also
                # converts any remaining sending rows without automatic replay.
                await asyncio.shield(self.queue.finish(row, 'uncertain', 'send_uncertain'))
                raise
            except Exception:
                logger.exception('Broadcast worker delivery failed | owner=%s job=%s customer=%s',
                                 row['owner'], row['job_id'], row['customer'])
                # Persistence or unexpected errors stop the worker. Do not
                # continue sending while acknowledgements cannot be trusted.
                raise
            logger.info('Broadcast result | owner=%s job=%s customer=%s connection=%s status=%s category=%s',
                        row['owner'], row['job_id'], row['customer'], cid, status, category)
            return True

    async def worker(self, application):
        stop = application.bot_data['broadcast_stop_event']
        while not stop.is_set():
            self.wakeup.clear()
            try:
                processed = await self.process_one(application)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Broadcast worker stopped; financial Outbox remains independent')
                return
            # At most one broadcast per second. Waking only bypasses the idle
            # delay; it never bypasses the post-send spacing or RetryAfter.
            if processed:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1.2)
                except TimeoutError:
                    pass
            else:
                waits = [asyncio.create_task(stop.wait()), asyncio.create_task(self.wakeup.wait())]
                try:
                    await asyncio.wait(waits, timeout=2, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in waits:
                        task.cancel()
                    await asyncio.gather(*waits, return_exceptions=True)
