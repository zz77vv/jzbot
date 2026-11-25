"""Tests for LedgerRepository: memos, undo/void, alias/tags, reports, CSV export, backup."""

from pathlib import Path

from ledgerflow.export import export_contacts_csv, export_journal_csv
from ledgerflow.repository import LedgerRepository


def test_post_memo_void_and_dedup(tmp_path: Path):
    repo = LedgerRepository(tmp_path / "lf.db")
    try:
        repo.upsert_link("L1", 10, True)
        r1 = repo.post_entry(owner_id=10, chat_id=200, tg_msg_id=1, link_id="L1", kind="in", amount_tenths=1000, note="货款", first_name="Alice")
        assert r1.applied and r1.balance_after == 1000

        # Dedup same tg_msg_id
        dup = repo.post_entry(owner_id=10, chat_id=200, tg_msg_id=1, link_id="L1", kind="in", amount_tenths=1000)
        assert not dup.applied and dup.duplicate

        # Out -250
        r2 = repo.post_entry(owner_id=10, chat_id=200, tg_msg_id=2, link_id="L1", kind="out", amount_tenths=250, note="返点")
        assert r2.applied and r2.balance_after == 750

        # Void last entry (-250) -> balance back to 1000!
        u1 = repo.void_last_entry(owner_id=10, chat_id=200, actor_id=10, tg_msg_id=3, link_id="L1")
        assert u1.applied and u1.balance_after == 1000
        c = repo.get_contact(10, 200)
        assert c.balance_tenths == 1000
        assert c.total_in_tenths == 1000
        assert c.total_out_tenths == 0

        # Void again -> voids +1000 -> balance 0
        u2 = repo.void_last_entry(owner_id=10, chat_id=200, actor_id=10)
        assert u2.applied and u2.balance_after == 0

        # Nothing left to void
        u3 = repo.void_last_entry(owner_id=10, chat_id=200, actor_id=10)
        assert not u3.applied and u3.reason == "nothing_to_void"
    finally:
        repo.close()


def test_crm_alias_tags_reports_and_csv_export(tmp_path: Path):
    repo = LedgerRepository(tmp_path / "lf.db")
    try:
        repo.upsert_link("L1", 1, True)
        repo.post_entry(owner_id=1, chat_id=101, tg_msg_id=1, link_id="L1", kind="in", amount_tenths=5000, note="首单", first_name="Bob")
        repo.post_entry(owner_id=1, chat_id=102, tg_msg_id=1, link_id="L1", kind="out", amount_tenths=1200, first_name="Carol")

        assert repo.set_alias(1, 101, "大客户老王")
        assert repo.set_tags(1, 101, ["VIP", "北京"])
        assert repo.list_owner_tags(1) == ["VIP", "北京"]

        # Search by custom alias & tag
        found, cnt = repo.search_contacts(1, "老王")
        assert cnt == 1 and found[0].label == "大客户老王"
        tagged, tcnt = repo.list_contacts(1, "tag:VIP")
        assert tcnt == 1 and tagged[0].chat_id == 101

        # Admin direct adjustment
        adj = repo.admin_adjust(owner_id=1, chat_id=101, delta_tenths=-500, note="手动调减")
        assert adj.applied and adj.balance_after == 4500

        # Financial report
        rep = repo.get_financial_report(1, "today")
        assert rep.total_in == 5000
        assert rep.total_out == 1700
        assert rep.net_flow == 3300
        assert rep.active_contacts == 2

        # CSV export (UTF-8 BOM)
        csv_bal = export_contacts_csv(repo.list_all_contacts_for_export(1))
        assert csv_bal.startswith(b"\xef\xbb\xbf")
        assert "大客户老王".encode("utf-8") in csv_bal

        csv_j = export_journal_csv(repo.get_all_journal_for_export(1))
        assert "手动调减".encode("utf-8") in csv_j

        # Hot backup
        bak = repo.hot_backup(tmp_path / "bak" / "lf-hot.db")
        b_repo = LedgerRepository(bak)
        try:
            assert b_repo.health_check() == (True, 0)
            assert b_repo.get_contact(1, 101).balance_tenths == 4500
        finally:
            b_repo.close()
    finally:
        repo.close()
