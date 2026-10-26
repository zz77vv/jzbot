from __future__ import annotations

import logging

from telegram.error import (
    TelegramError,
)
from telegram.ext import ContextTypes

from jzbot.domain import (
    LocateFailureCategory,
    LocateResult,
    classify_locate_error,
    log_locate_failure,
)
from jzbot.infra.business import send_business_text

logger = logging.getLogger("jzbot")


class ConnectionsService:
    def __init__(self, store):
        self.store = store
        self.send_lock = store.delivery_lock

    async def ensure_connection_info(
        self, context: ContextTypes.DEFAULT_TYPE, business_connection_id: str
    ):
        known = await self.store.get_connection_owner(business_connection_id)
        if known is not None:
            return known
        try:
            connection = await context.bot.get_business_connection(
                business_connection_id
            )
        except TelegramError:
            logger.exception(
                "get_business_connection failed: %s", business_connection_id
            )
            return None
        await self.store.upsert_business_connection(
            connection.id, connection.user.id, connection.is_enabled
        )
        return (connection.user.id, connection.is_enabled)

    async def locate_customer_chat(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        owner_user_id: int,
        customer_chat_id: int,
    ) -> LocateResult:
        """
        给客户静默发送一个句点，让该私聊重新出现在 Business 账号的最近聊天中。

        每次定位前刷新候选 Business 连接，只使用当前仍启用且可回复的连接。
        返回结构化结果，由 callback 显示简洁、保守的管理员提示。
        """
        connection_ids = await self.store.get_enabled_connection_ids(owner_user_id)
        if not connection_ids:
            return LocateResult(
                success=False, failure_category=LocateFailureCategory.NO_CONNECTION
            )
        failures: list[LocateFailureCategory] = []
        for connection_id in connection_ids[:8]:
            try:
                connection = await context.bot.get_business_connection(connection_id)
            except TelegramError as exc:
                category = classify_locate_error(exc)
                failures.append(category)
                log_locate_failure(
                    owner_user_id, customer_chat_id, connection_id, category, exc
                )
                if category in {
                    LocateFailureCategory.CONNECTION_INVALID,
                    LocateFailureCategory.CONNECTION_DISABLED,
                }:
                    await self.store.mark_business_connection_disabled(
                        connection_id, owner_user_id
                    )
                continue
            actual_owner_id = int(connection.user.id)
            if actual_owner_id != owner_user_id:
                failures.append(LocateFailureCategory.CONNECTION_INVALID)
                logger.error(
                    "Locate connection owner mismatch | owner=%s customer=%s business_connection=%s actual_owner=%s failure_category=%s",
                    owner_user_id,
                    customer_chat_id,
                    connection_id,
                    actual_owner_id,
                    LocateFailureCategory.CONNECTION_INVALID.value,
                )
                await self.store.mark_business_connection_disabled(
                    connection_id, owner_user_id
                )
                continue
            await self.store.upsert_business_connection(
                connection.id, actual_owner_id, connection.is_enabled
            )
            if not connection.is_enabled:
                failures.append(LocateFailureCategory.CONNECTION_DISABLED)
                logger.warning(
                    "Locate connection disabled | owner=%s customer=%s business_connection=%s failure_category=%s",
                    owner_user_id,
                    customer_chat_id,
                    connection_id,
                    LocateFailureCategory.CONNECTION_DISABLED.value,
                )
                continue
            rights = getattr(connection, "rights", None)
            can_reply = getattr(rights, "can_reply", None)
            if can_reply is False:
                failures.append(LocateFailureCategory.NO_REPLY_PERMISSION)
                logger.warning(
                    "Locate connection cannot reply | owner=%s customer=%s business_connection=%s failure_category=%s",
                    owner_user_id,
                    customer_chat_id,
                    connection_id,
                    LocateFailureCategory.NO_REPLY_PERMISSION.value,
                )
                continue
            try:
                await send_business_text(
                    context,
                    connection_id,
                    customer_chat_id,
                    ".",
                    disable_notification=True,
                )
                logger.info(
                    "Locate chat sent | owner=%s customer=%s business_connection=%s",
                    owner_user_id,
                    customer_chat_id,
                    connection_id,
                )
                return LocateResult(success=True, connection_id=connection_id)
            except TelegramError as exc:
                category = classify_locate_error(exc)
                failures.append(category)
                log_locate_failure(
                    owner_user_id, customer_chat_id, connection_id, category, exc
                )
                if category in {
                    LocateFailureCategory.CONNECTION_INVALID,
                    LocateFailureCategory.CONNECTION_DISABLED,
                }:
                    await self.store.mark_business_connection_disabled(
                        connection_id, owner_user_id
                    )
        priority = (
            LocateFailureCategory.CUSTOMER_BLOCKED,
            LocateFailureCategory.CUSTOMER_UNREACHABLE,
            LocateFailureCategory.NO_REPLY_PERMISSION,
            LocateFailureCategory.TEMPORARY,
            LocateFailureCategory.CONNECTION_INVALID,
            LocateFailureCategory.CONNECTION_DISABLED,
            LocateFailureCategory.UNKNOWN,
        )
        final_category = next(
            (category for category in priority if category in failures),
            LocateFailureCategory.NO_CONNECTION,
        )
        return LocateResult(success=False, failure_category=final_category)
