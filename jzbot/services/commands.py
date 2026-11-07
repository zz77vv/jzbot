"""Persist trusted mutations before accounting, replaying the original source key."""

import asyncio
import contextlib
import logging
import time

from jzbot.repositories.commands import CommandRepository

logger = logging.getLogger("jzbot")
NOTICE_SEND_TIMEOUT = 15


class CommandService:
    def __init__(self, accounting, outbox, path):
        if path.resolve() == accounting.store.db_path.resolve():
            raise ValueError("Command queue must be separate from the ledger")
        self.accounting = accounting
        self.outbox = outbox
        self.queue = CommandRepository(path, accounting.store.db_path)
        self.processing = asyncio.Lock()
        self.wakeup = asyncio.Event()
        self.notice_processing = asyncio.Lock()
        self.notice_wakeup = asyncio.Event()

    async def _emergency_notice(self, context, owner, customer, source):
        try:
            async with asyncio.timeout(NOTICE_SEND_TIMEOUT):
                await context.bot.send_message(
                    chat_id=owner,
                    text=f"客户 {customer}，消息 {source}：命令处理状态未确认，"
                    "恢复队列暂不可用。请核对账本及原消息，勿另发同额命令；"
                    "已保存的原命令会在存储恢复后自动重试。",
                )
        except Exception:
            logger.exception("Command owner alert unavailable | owner=%s source=%s", owner, source)

    async def process_notices(self, context, *, now=None):
        # Notification requests never acquire the accounting processing lock.
        now = time.time() if now is None else now
        async with self.notice_processing:
            await self._flush_notices(context, now)

    async def _flush_notices(self, context, now):
        for row in await self.queue.notices(now):
            text = ("命令已保存，处理状态待确认，将自动重试。请勿另发同额命令；"
                    "本客户后续记账命令按顺序等待。")
            if row["notice"] == "recovered":
                text = "原命令已确认入账；客户回执由回执队列独立发送。"
            try:
                async with asyncio.timeout(NOTICE_SEND_TIMEOUT):
                    await context.bot.send_message(
                        chat_id=row["owner"],
                        text=f"客户 {row['customer']}，消息 {row['source']}：{text}",
                    )
            except Exception:
                logger.exception("Command owner notice deferred | owner=%s source=%s",
                                 row["owner"], row["source"])
                await self.queue.finish_notice(row, now, sent=False)
            else:
                await self.queue.finish_notice(row, now, sent=True)

    async def submit(self, context, owner, customer, source, connection, *, delta=None, clear=False):
        # Only the handler's authenticated owner messages enter this boundary.
        # Replay still rechecks current connection authorization in accounting.
        try:
            row = await self.queue.enqueue(owner, customer, source, connection,
                                           "clear" if clear else "amount", delta)
            if row["status"] == "done":
                self.outbox.notify()
                return
            await self.process_one(context, scope=(owner, customer))
            current = await self.queue.get(owner, customer, source)
            if current["status"] == "pending":
                await self.queue.defer(current)
                self.notice_wakeup.set()
        except asyncio.CancelledError:
            # Before/after either commit the persisted original source remains
            # safe to replay. Never turn cancellation into a success response.
            raise
        except Exception:
            logger.exception("Command persistence unavailable | owner=%s chat=%s source=%s",
                             owner, customer, source)
            await self._emergency_notice(context, owner, customer, source)
        finally:
            self.wakeup.set()

    async def process_one(self, context, *, now=None, scope=None):
        now = time.time() if now is None else now
        async with self.processing:
            row = await self.queue.next_due(now, scope)
            if row is None:
                return False
            try:
                if row["kind"] == "clear":
                    await self.accounting.clear_balance(
                        row["owner"], row["customer"], row["source"], row["connection"],
                    )
                else:
                    await self.accounting.apply_amount(
                        row["owner"], row["customer"], row["source"], int(row["delta"]),
                        row["connection"],
                    )
                # Notification is only a wakeup; actual customer sends belong to
                # Outbox. A queue ack failure must replay, never compensate.
                self.outbox.notify()
                await self.queue.complete(row)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Trusted command deferred | owner=%s chat=%s source=%s",
                                 row["owner"], row["customer"], row["source"])
                await self.queue.failed(row, now, type(exc).__name__)
            self.notice_wakeup.set()
            return True

    async def worker(self, application):
        notices = asyncio.create_task(self.notice_worker(application), name="trusted-command-notices")
        application.bot_data["command_notice_worker_task"] = notices
        try:
            await self._command_worker(application)
        finally:
            notices.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await notices

    async def notice_worker(self, application):
        stop = application.bot_data["command_stop_event"]
        while not stop.is_set():
            self.notice_wakeup.clear()
            try:
                await self.process_notices(application)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Command notice storage unavailable; retrying independently")
            waits = [asyncio.create_task(stop.wait()), asyncio.create_task(self.notice_wakeup.wait())]
            try:
                await asyncio.wait(waits, timeout=2, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in waits:
                    task.cancel()
                await asyncio.gather(*waits, return_exceptions=True)

    async def _command_worker(self, application):
        stop = application.bot_data["command_stop_event"]
        while not stop.is_set():
            self.wakeup.clear()
            try:
                if await self.process_one(application):
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Trusted command worker storage unavailable; retrying")
            waits = [asyncio.create_task(stop.wait()), asyncio.create_task(self.wakeup.wait())]
            try:
                await asyncio.wait(waits, timeout=2, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in waits:
                    task.cancel()
                await asyncio.gather(*waits, return_exceptions=True)
