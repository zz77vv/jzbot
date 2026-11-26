"""aiogram 3 router for Telegram Business connections and private-chat bookkeeping."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BusinessConnection, Message, ReplyParameters

from ledgerflow.domain import esc, fmt_money, parse_chat_intent
from ledgerflow.repository import LedgerRepository
from ledgerflow.ui import format_tx_line
from ledgerflow.workers import ReceiptDispatcher

logger = logging.getLogger(__name__)


def render_statement_card(repo: LedgerRepository, owner_id: int, chat_id: int, tz_offset: int = 8) -> str:
    c = repo.get_contact(owner_id, chat_id)
    unit = repo.currency_unit
    if not c or not c.has_ledger:
        return f"<b>客户对账单</b>\n\n<blockquote>当前余额　<b><code>0 {esc(unit)}</code></b>\n<i>暂无记账流水</i></blockquote>"

    txs, total = repo.get_journal(owner_id, chat_id, limit=8, offset=0)
    bal_sign = "+" if c.balance_tenths > 0 else ""
    tag_line = ("  " + " ".join(f"<code>#{esc(t)}</code>" for t in c.tags)) if c.tags else ""
    summary = (
        f"<b>{esc(c.label)}</b>{tag_line}\n\n"
        "<blockquote><b>账本摘要</b>\n"
        f"当前余额　<b><code>{bal_sign}{fmt_money(c.balance_tenths, unit)}</code></b>\n"
        f"累计入账　<code>+{fmt_money(c.total_in_tenths, unit)}</code>  ·  累计出账　<code>-{fmt_money(c.total_out_tenths, unit)}</code></blockquote>"
    )
    if not txs:
        return summary + "\n\n<blockquote><i>暂无流水记录</i></blockquote>"

    tx_lines = "\n".join(format_tx_line(tx, unit, tz_offset, show_id=False) for tx in txs)
    return summary + f"\n\n<blockquote expandable><b>最近 {len(txs)} 笔流水（共 {total} 笔 · 点击展开）</b>\n{tx_lines}</blockquote>"


def create_business_router(repo: LedgerRepository, receipts: ReceiptDispatcher, tz_offset: int = 8) -> Router:
    router = Router(name="business")

    @router.business_connection()
    async def on_business_connection(event: BusinessConnection, bot: Bot) -> None:
        if not event.user:
            return
        repo.upsert_link(event.id, event.user.id, bool(event.is_enabled))
        logger.info("Business link %s updated for owner %s (enabled=%s)", event.id, event.user.id, event.is_enabled)
        if event.is_enabled and repo.mark_welcomed(event.user.id):
            try:
                await bot.send_message(
                    chat_id=event.user.id,
                    parse_mode=ParseMode.HTML,
                    text=(
                        "⚡ <b>LedgerFlow 智能记账已就绪</b>\n\n"
                        "在任意客户私聊中直接发送：\n"
                        "• <code>+100</code> 或 <code>+100 换U</code> — 入账（支持备注）\n"
                        "• <code>-50.5</code> 或 <code>-50.5 结款</code> — 出账\n"
                        "• <code>撤销</code>（或 <code>撤</code>）— 冲正撤销上一笔\n"
                        "• <code>/</code> 或 <code>账单</code> — 查看对账单\n"
                        "• <code>//</code> 或 <code>清账</code> — 结清归零\n\n"
                        "私聊本机器人发送 /start 打开财务总控台、报表与导出中心。"
                    ),
                )
            except TelegramAPIError as exc:
                logger.info("Welcome skipped for owner %s: %s", event.user.id, exc)

    @router.business_message(F.chat.type == ChatType.PRIVATE, F.text)
    async def on_business_message(message: Message, bot: Bot) -> None:
        link_id = message.business_connection_id
        if not link_id:
            return

        link_info = repo.get_link(link_id)
        if link_info is None:
            try:
                remote = await bot.get_business_connection(link_id)
                repo.upsert_link(remote.id, remote.user.id, bool(remote.is_enabled))
                link_info = (remote.user.id, bool(remote.is_enabled))
            except TelegramAPIError as exc:
                logger.warning("Cannot resolve business link %s: %s", link_id, exc)
                return

        owner_id, enabled = link_info
        if not enabled:
            return

        chat = message.chat
        if chat.id != owner_id:
            repo.sync_contact_profile(owner_id, chat.id, chat.username, chat.first_name, chat.last_name)

        sender = message.from_user
        if not sender or sender.is_bot or sender.id != owner_id:
            return

        intent = parse_chat_intent(message.text)
        if intent is None:
            return

        if intent.kind == "query":
            card = render_statement_card(repo, owner_id, chat.id, tz_offset)
            try:
                await bot.send_message(
                    chat_id=chat.id,
                    text=card,
                    parse_mode=ParseMode.HTML,
                    business_connection_id=link_id,
                    reply_parameters=ReplyParameters(message_id=message.message_id, allow_sending_without_reply=True),
                )
            except TelegramAPIError as exc:
                logger.warning("Failed sending statement card to %s: %s", chat.id, exc)
            return

        if intent.kind in ("in", "out"):
            try:
                res = repo.post_entry(
                    owner_id=owner_id,
                    chat_id=chat.id,
                    tg_msg_id=message.message_id,
                    link_id=link_id,
                    kind=intent.kind,
                    amount_tenths=intent.amount_tenths,
                    note=intent.note,
                    username=chat.username,
                    first_name=chat.first_name,
                    last_name=chat.last_name,
                )
            except OverflowError:
                logger.warning("Ignored overflow amount for owner %s chat %s", owner_id, chat.id)
                return
            if res.applied:
                receipts.wake()
            return

        if intent.kind == "undo":
            res = repo.void_last_entry(
                owner_id=owner_id,
                chat_id=chat.id,
                actor_id=owner_id,
                tg_msg_id=message.message_id,
                link_id=link_id,
            )
            if res.applied:
                receipts.wake()
            elif res.reason == "nothing_to_void":
                try:
                    await bot.send_message(
                        chat_id=chat.id,
                        text="ℹ️ 暂无可撤销的有效入账/出账流水。",
                        business_connection_id=link_id,
                        reply_parameters=ReplyParameters(message_id=message.message_id, allow_sending_without_reply=True),
                    )
                except TelegramAPIError:
                    pass
            return

        if intent.kind == "settle":
            res = repo.settle_balance(
                owner_id=owner_id,
                chat_id=chat.id,
                actor_id=owner_id,
                tg_msg_id=message.message_id,
                link_id=link_id,
            )
            if res.applied:
                receipts.wake()

    return router
