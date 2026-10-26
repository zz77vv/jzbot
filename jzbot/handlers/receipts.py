from datetime import datetime, timezone

from jzbot.services.recovery import ReceiptRecoveryService
from jzbot.ui.pages import safe_edit_query
from jzbot.ui.receipts import pending_receipts_view, recovery_confirmation_view


class ReceiptHandlers:
    def __init__(self, store):
        self.store = store
        self.recovery = ReceiptRecoveryService(store)

    async def page(self, query, owner, page, notice=None):
        rows, total, page = await self.store.get_pending_receipts_page(owner, page)
        await safe_edit_query(query, *pending_receipts_view(rows, total, page, notice))

    async def on_callback(self, query, user, context, data):
        if not data.startswith(
            ("receipts:", "receipt_check:", "receipt_confirm:", "receipt_cancel:")
        ):
            return False
        # Keep independent of the outer router's guard for safe direct use.
        if (
            query.message is None
            or query.inline_message_id
            or query.message.chat_id != user.id
            or not await self.store.is_business_owner(user.id)
        ):
            await query.answer("无权操作此回执", show_alert=True)
            return True
        await query.answer()
        requests = context.user_data.setdefault("receipt_recovery_requests", {})
        parts = data.split(":")
        try:
            if parts[0] == "receipts" and len(parts) == 2:
                await self.page(query, user.id, max(0, min(int(parts[1]), 999999)))
            elif parts[0] == "receipt_check" and len(parts) == 3:
                request = await self.recovery.prepare(
                    context, user.id, int(parts[1]), int(parts[2])
                )
                if request is None:
                    await self.page(
                        query,
                        user.id,
                        0,
                        "任务已完成、无权访问或暂时没有有效连接，请检查 Business 权限。",
                    )
                else:
                    now = datetime.now(timezone.utc)
                    for nonce, old in list(requests.items()):
                        if now >= datetime.fromisoformat(old["expires"]):
                            requests.pop(nonce, None)
                    while len(requests) >= 16:
                        requests.pop(next(iter(requests)))
                    requests[request["nonce"]] = request
                    await safe_edit_query(query, *recovery_confirmation_view(request))
            elif parts[0] in {"receipt_confirm", "receipt_cancel"} and len(parts) == 2:
                request = requests.pop(parts[1], None)
                if request is None or request["owner"] != user.id:
                    await self.page(
                        query, user.id, 0, "确认已过期或已处理，请重新检查。"
                    )
                elif parts[0] == "receipt_cancel":
                    await self.page(
                        query, user.id, request["page"], "已取消，回执任务未修改。"
                    )
                else:
                    result = await self.recovery.confirm(context, user.id, request)
                    notices = {
                        "checked": "连接已核验，将按原计划自动投递。",
                        "rebound": "连接已恢复，将按原计划自动投递。",
                        "expired": "确认已过期，请重新检查。",
                        "stale": "任务已变化或已完成，未修改回执。",
                        "unavailable": "连接当前不可用，请检查 Business 回复权限。",
                    }
                    await self.page(query, user.id, request["page"], notices[result])
            else:
                raise ValueError("invalid receipt callback")
        except (ValueError, KeyError):
            await self.page(query, user.id, 0, "按钮已过期，请刷新后重试。")
        return True
