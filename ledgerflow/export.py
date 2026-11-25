"""UTF-8 BOM CSV exporters for Excel-compatible financial statements."""

from __future__ import annotations

import csv
import io

from ledgerflow.domain import fmt_local_dt, fmt_plain_num
from ledgerflow.repository import Contact, JournalEntry

KIND_LABELS = {
    "in": "入账",
    "out": "出账",
    "void": "撤销冲正",
    "settle": "清账结清",
    "adjust": "后台调账",
}


def export_contacts_csv(contacts: list[Contact], unit: str = "USDT", tz_offset: int = 8) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["客户ID", "自定义别名", "Telegram名称", "用户名", "标签", f"当前余额({unit})", f"累计入账({unit})", f"累计出账({unit})", "流水笔数", "归档", "最后更新时间"])
    for c in contacts:
        writer.writerow(
            [
                c.chat_id,
                c.alias or "",
                c.tg_display_name,
                f"@{c.tg_username}" if c.tg_username else "",
                ",".join(c.tags),
                fmt_plain_num(c.balance_tenths),
                fmt_plain_num(c.total_in_tenths),
                fmt_plain_num(c.total_out_tenths),
                c.tx_count,
                "是" if c.archived else "否",
                fmt_local_dt(c.updated_at, tz_offset),
            ]
        )
    return buf.getvalue().encode("utf-8-sig")


def export_journal_csv(entries: list[tuple[JournalEntry, str]], unit: str = "USDT", tz_offset: int = 8) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["流水号", "时间", "客户ID", "客户名称", "类型", f"变动金额({unit})", f"变动前余额({unit})", f"变动后余额({unit})", "备注", "状态"])
    for tx, label in entries:
        status = f"已被 #{tx.voided_by_id} 撤销" if tx.voided_by_id else "有效"
        writer.writerow(
            [
                tx.id,
                fmt_local_dt(tx.created_at, tz_offset),
                tx.chat_id,
                label,
                KIND_LABELS.get(tx.kind, tx.kind),
                fmt_plain_num(tx.delta_tenths),
                fmt_plain_num(tx.balance_before),
                fmt_plain_num(tx.balance_after),
                tx.note,
                status,
            ]
        )
    return buf.getvalue().encode("utf-8-sig")
