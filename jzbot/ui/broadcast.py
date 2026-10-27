from html import escape

from telegram import InlineKeyboardMarkup

from jzbot.domain import admin_page_bounds
from jzbot.services.broadcast import AUDIENCES, MAX_TEXT_UNITS
from jzbot.ui.pages import (
    admin_button,
    admin_navigation,
    admin_pagination,
    admin_rich_page,
    admin_rich_table,
)

JOB_LABELS = {'running': '发送中', 'paused': '已暂停', 'cancelled': '已取消', 'completed': '已完成'}
RESULT_LABELS = {'pending': '等待发送', 'sending': '正在发送', 'sent': '已发送',
                 'failed': '发送失败', 'uncertain': '结果不确定', 'cancelled': '已取消'}
REASONS = {
    'no_connection': '没有可用 Business 连接',
    'connection_invalid': 'Business 连接失效',
    'connection_disabled': 'Business 连接已停用',
    'no_reply_permission': 'Business 代发权限不足',
    'customer_unreachable': '客户当前不符合可回复条件或聊天不可发送',
    'customer_blocked': '客户当前不允许机器人发消息',
    'temporary': '连接检查暂时失败',
    'rate_limit': 'Telegram 限流，按要求等待',
    'send_uncertain': '发送结果不确定，未自动重发',
    'restart_uncertain': '重启前发送结果不确定，未自动重发',
    'unknown': 'Telegram 拒绝发送，原因未识别',
}


def broadcast_home(rows, total, page, *, worker_alive=True):
    page, pages = admin_page_bounds(total, page)
    body = '<h2>客户广播</h2><p>先预览并确认，再发送给所选客户。</p>'
    if not worker_alive:
        body += '<p><b>广播发送服务未运行，请联系维护；记账回执不受影响。</b></p>'
    buttons = [[admin_button('新建广播', 'bc:new', 'primary')]]
    for row in rows:
        label = JOB_LABELS[row['status']]
        buttons.append([admin_button(f"{label} · {row['body'][:20]}", f"bc:job:{row['id']}:0")])
    if not rows:
        body += '<p>暂无广播记录</p>'
    buttons += admin_pagination(page, pages, lambda p: f'bc:list:{p}')
    buttons += [admin_navigation()]
    return admin_rich_page(body, InlineKeyboardMarkup(buttons), f'共 {total} 条｜第 {page+1}/{pages} 页 · 仅当前管理员可见')


def audience_view(selected, counts):
    body = '<h2>选择广播客户</h2>' + admin_rich_table([(label, f'{counts[k]} 人') for k, label in AUDIENCES.items()])
    body += '<p>可以多选，客户自动去重。余额为 0 归入存款用户。<br>无账本、无流水的资料不会仅因余额缺省为 0 被选入。</p>'
    buttons = [[admin_button(('✓ ' if key in selected else '') + label, f'bc:toggle:{key}')]
               for key, label in AUDIENCES.items()]
    buttons += [[admin_button('下一步：输入内容', 'bc:compose', 'primary')],
                [admin_button('取消', 'bc:cancel')], admin_navigation('bc:list:0')]
    return admin_rich_page(body, InlineKeyboardMarkup(buttons), '范围按当前账本筛选 · 包含原有内部状态标记的客户')


def compose_view(notice=None):
    body = '<h2>输入广播内容</h2><p>请发送一条纯文本消息，可保留换行。</p>'
    if notice:
        body += '<p>' + escape(notice) + '</p>'
    return admin_rich_page(body, InlineKeyboardMarkup([
        [admin_button('返回选择范围', 'bc:audience'), admin_button('取消', 'bc:cancel')],
        admin_navigation('bc:list:0'),
    ]), f'最多 {MAX_TEXT_UNITS} 个字符单位 · 尚未向客户发送')


def preview_view(request):
    labels = '、'.join(AUDIENCES[k] for k in request['selected'])
    names = '、'.join(escape(r['name'][:24]) for r in request['recipients'][:5])
    body = '<h2>确认广播</h2>' + admin_rich_table([
        ('范围', labels), ('去重后接收人数', f"{len(request['recipients'])} 人"),
        ('发送方式', '逐人静默发送，不置顶'),
    ])
    body += '<h3>完整正文</h3><p>' + escape(request['body']).replace('\n', '<br>') + '</p>'
    body += '<p>名单抽查：' + names + '</p>'
    body += '<p>Telegram 只允许联系符合 Business 回复条件的客户，可能有部分发送失败。发送超时的客户不会自动重发。</p>'
    nonce = request['id']
    return admin_rich_page(body, InlineKeyboardMarkup([
        [admin_button(f"确认发送给 {len(request['recipients'])} 人", f'bc:confirm:{nonce}', 'danger')],
        [admin_button('重新编辑', 'bc:compose'), admin_button('取消', 'bc:cancel')],
        admin_navigation('bc:list:0'),
    ]), '确认 5 分钟有效 · 确认后固定接收名单与正文')


def progress_view(job, rows, page, notice=None):
    page, pages = admin_page_bounds(job['total'], page)
    body = '<h2>广播结果</h2>' + admin_rich_table([
        ('任务状态', JOB_LABELS[job['status']]), ('接收人数', f"{job['total']} 人"),
        *[(RESULT_LABELS[key], f"{job['counts'].get(key,0)} 人")
          for key in ('sent','pending','sending','failed','uncertain','cancelled')],
    ])
    if notice:
        body += '<p>' + escape(notice) + '</p>'
    body += '<details><summary>查看广播正文</summary><p>' + escape(job['body']).replace('\n','<br>') + '</p></details>'
    for row in rows:
        reason = REASONS.get(row['category'], '未识别原因') if row['category'] else ''
        body += f"<p>{escape(row['name'][:40])} · {RESULT_LABELS[row['status']]}"
        if reason:
            body += '<br>' + escape(reason)
        body += '</p>'
    buttons = admin_pagination(page, pages, lambda p: f"bc:job:{job['id']}:{p}")
    buttons.append([admin_button('刷新结果', f"bc:job:{job['id']}:{page}")])
    controls = []
    if job['status'] == 'running':
        controls.append(admin_button('暂停', f"bc:pause:{job['id']}"))
    if job['status'] == 'paused':
        controls.append(admin_button('继续未发送部分', f"bc:resume:{job['id']}", 'primary'))
    if job['status'] in ('running','paused'):
        controls.append(admin_button('取消未发送部分', f"bc:stop:{job['id']}", 'danger'))
    if controls:
        buttons.append(controls)
    buttons.append(admin_navigation('bc:list:0', '返回广播'))
    return admin_rich_page(body, InlineKeyboardMarkup(buttons),
                           f'第 {page+1}/{pages} 页 · 已发送仅表示 API 返回成功，不代表已读 · 暂停/取消无法撤回已发消息')
