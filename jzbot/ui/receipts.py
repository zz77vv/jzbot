from html import escape

from telegram import InlineKeyboardMarkup

from jzbot.domain import (
    admin_page_bounds,
    beijing_display_from_utc,
    customer_display_name,
)

from .pages import (
    admin_button,
    admin_navigation,
    admin_pagination,
    admin_rich_page,
    admin_rich_table,
    outbox_error_summary,
)


def pending_receipts_view(rows, total, page, notice=None):
    page, pages = admin_page_bounds(total, page)
    body = f"<h2>待交付回执</h2><p>共 {total} 条｜第 {page + 1} / {pages} 页</p>"
    if notice:
        body += "<p>" + escape(notice) + "</p>"
    buttons = []
    for row in rows:
        body += (
            f"<p><b>{escape(customer_display_name(row)[:40])}</b><br>"
            f"失败 {row['attempt_count']} 次 · {outbox_error_summary(row['last_error'])}<br>"
            f"下次尝试 {beijing_display_from_utc(row['next_attempt_at_utc'])}</p>"
        )
        buttons.append(
            [admin_button("检查 / 恢复此回执连接", f"receipt_check:{row['id']}:{page}")]
        )
    if not rows:
        body += "<p>暂无待交付回执</p>"
    buttons += admin_pagination(page, pages, lambda n: f"receipts:{n}")
    buttons += [[admin_button("刷新", f"receipts:{page}")], admin_navigation()]
    return admin_rich_page(
        body,
        InlineKeyboardMarkup(buttons),
        "刚创建的等待任务不等于故障 · 发送超时可能产生重复回执",
    )


def recovery_confirmation_view(request):
    body = "<h2>确认恢复回执连接</h2>" + admin_rich_table(
        [
            ("操作", "重新核验有效 Business 连接"),
            ("账务", "不重新记账、不修改余额"),
            ("投递", "保留原重试时间、状态及失败次数"),
        ]
    )
    body += "<p>只修复连接归属，不立即发送。若上次发送结果不确定，后续自动重试仍可能产生重复回执。</p>"
    nonce = request["nonce"]
    return admin_rich_page(
        body,
        InlineKeyboardMarkup(
            [
                [
                    admin_button(
                        "确认检查 / 恢复", "receipt_confirm:" + nonce, "primary"
                    ),
                    admin_button("取消", "receipt_cancel:" + nonce),
                ],
                admin_navigation(),
            ]
        ),
        "确认 5 分钟有效 · 回执已投递或任务变化时不再修改",
    )
