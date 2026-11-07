import sqlite3
import time

from jzbot.domain import admin_page_bounds
from jzbot.infra.rich import send_admin_rich
from jzbot.services.broadcast import AUDIENCES
from jzbot.ui.broadcast import (
    audience_view,
    broadcast_home,
    compose_view,
    preview_view,
    progress_view,
)
from jzbot.ui.pages import safe_edit_query


class BroadcastHandlers:
    def __init__(self, service):
        self.service = service

    async def allowed(self, user, chat):
        return (user is not None and chat is not None and chat.type == 'private'
                and chat.id == user.id and await self.service.store.is_business_owner(user.id))

    async def home(self, query, owner, page, context):
        rows, total = await self.service.queue.list_jobs(owner, page)
        page, _ = admin_page_bounds(total, page)
        rows, total = await self.service.queue.list_jobs(owner, page)
        worker = context.bot_data.get('broadcast_worker_task')
        await safe_edit_query(query, *broadcast_home(rows, total, page, worker_alive=worker is not None and not worker.done()))

    async def audience(self, query, owner, context):
        selected = context.user_data.get('broadcast_selected', [])
        counts = await self.service.audience_counts(owner)
        await safe_edit_query(query, *audience_view(selected, counts))

    async def progress(self, query, owner, job_id, page=0, notice=None):
        job = await self.service.queue.get(owner, job_id)
        if job is None:
            await safe_edit_query(query, *compose_view('广播不存在或无权查看，请返回广播列表。'))
            return
        page, _ = admin_page_bounds(job['total'], page)
        rows, _ = await self.service.queue.recipient_page(owner, job_id, page)
        await safe_edit_query(query, *progress_view(job, rows, page, notice))

    async def on_text(self, update, context):
        if not context.user_data.get('awaiting_broadcast_text'):
            return False
        if not await self.allowed(update.effective_user, update.effective_chat) or update.message is None:
            return True
        if time.time() >= context.user_data.get('broadcast_input_expires', 0):
            context.user_data.pop('awaiting_broadcast_text', None)
            await send_admin_rich(context.bot, update.effective_chat.id, *compose_view('输入已超时，请从广播入口重新开始。'))
            return True
        try:
            request = await self.service.prepare(update.effective_user.id,
                context.user_data.get('broadcast_selected', []), update.message.text)
        except ValueError as exc:
            await send_admin_rich(context.bot, update.effective_chat.id, *compose_view(str(exc)))
            return True
        context.user_data.pop('awaiting_broadcast_text', None)
        context.user_data['broadcast_preview'] = request
        await send_admin_rich(context.bot, update.effective_chat.id, *preview_view(request))
        return True

    async def on_callback(self, query, user, context, data):
        if not data.startswith('bc:'):
            return False
        if (query.message is None or query.inline_message_id or query.message.chat_id != user.id
                or not await self.service.store.is_business_owner(user.id)):
            await query.answer('无权操作此广播。', show_alert=True)
            return True
        await query.answer()
        parts = data.split(':')
        try:
            action = parts[1]
            if action in ('new','audience') and len(parts) == 2:
                if action == 'new':
                    context.user_data['broadcast_selected'] = []
                context.user_data.pop('awaiting_customer_search', None)
                context.user_data.pop('awaiting_broadcast_text', None)
                context.user_data.pop('broadcast_preview', None)
                await self.audience(query, user.id, context)
            elif action == 'toggle' and len(parts) == 3 and parts[2] in AUDIENCES:
                selected = set(context.user_data.get('broadcast_selected', []))
                selected.symmetric_difference_update({parts[2]})
                context.user_data['broadcast_selected'] = sorted(selected)
                context.user_data.pop('broadcast_preview', None)
                await self.audience(query, user.id, context)
            elif action == 'compose' and len(parts) == 2:
                if not context.user_data.get('broadcast_selected'):
                    raise ValueError('请先选择广播客户范围。')
                context.user_data.pop('awaiting_customer_search', None)
                context.user_data.pop('broadcast_preview', None)
                context.user_data['awaiting_broadcast_text'] = True
                context.user_data['broadcast_input_expires'] = time.time() + 900
                await safe_edit_query(query, *compose_view())
            elif action == 'confirm' and len(parts) == 3:
                worker = context.bot_data.get('broadcast_worker_task')
                if worker is None or worker.done():
                    raise ValueError('广播发送服务未运行，请联系维护后重试。')
                request = context.user_data.get('broadcast_preview')
                if not request or request['id'] != parts[2] or request['owner'] != user.id:
                    # A repeat confirmation may refer to its already durable job.
                    job = await self.service.queue.get(user.id, parts[2])
                    if job:
                        await self.progress(query, user.id, parts[2])
                        return True
                    raise ValueError('确认已过期或已取消，请重新预览。')
                job_id = await self.service.confirm(user.id, request)
                context.user_data.pop('broadcast_preview', None)
                await self.progress(query, user.id, job_id)
            elif action in ('pause','resume','stop') and len(parts) == 3:
                if action == 'resume':
                    worker = context.bot_data.get('broadcast_worker_task')
                    if worker is None or worker.done():
                        raise ValueError('广播发送服务未运行，请联系维护。')
                changed = await self.service.queue.control(user.id, parts[2], {'stop':'cancel'}.get(action,action))
                self.service.wakeup.set()
                await self.progress(query, user.id, parts[2], notice='操作已生效；正在发送的一条可能已提交。' if changed else '任务状态已变化，请查看当前结果。')
            elif action == 'job' and len(parts) == 4:
                await self.progress(query, user.id, parts[2], max(0,min(int(parts[3]),999999)))
            elif action == 'list' and len(parts) == 3:
                context.user_data.pop('awaiting_broadcast_text', None)
                context.user_data.pop('broadcast_preview', None)
                await self.home(query, user.id, max(0,min(int(parts[2]),999999)), context)
            elif action == 'cancel' and len(parts) == 2:
                context.user_data.pop('awaiting_broadcast_text', None)
                context.user_data.pop('broadcast_preview', None)
                context.user_data.pop('broadcast_selected', None)
                await self.home(query, user.id, 0, context)
            else:
                raise ValueError('广播按钮已过期，请重新打开。')
        except (ValueError, KeyError, sqlite3.IntegrityError) as exc:
            notice = '已有发送中或暂停的广播，请先处理该任务。' if isinstance(exc, sqlite3.IntegrityError) else str(exc)
            await safe_edit_query(query, *compose_view(notice))
        return True

