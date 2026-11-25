"""Tests for Store atomic bookkeeping, admin ops, queries, and broadcasts."""

from pathlib import Path

from jzbot.store import Store


def test_bookkeeping_dedup_and_clear_rules(tmp_path: Path):
    store = Store(tmp_path / "jzbot.db")
    try:
        store.upsert_connection("conn1", 100, True)

        # Deposit +15
        r1 = store.record_amount(
            owner_id=100,
            chat_id=2001,
            source_message_id=1,
            connection_id="conn1",
            kind="deposit",
            amount_tenths=150,
            username="alice",
            first_name="Alice",
        )
        assert r1.applied is True
        assert r1.balance_before == 0
        assert r1.balance_after == 150
        assert r1.receipt_text == "入金 15 USDT, 余额 15 USDT"

        # Duplicate source_message_id must not double-count
        r1_dup = store.record_amount(
            owner_id=100,
            chat_id=2001,
            source_message_id=1,
            connection_id="conn1",
            kind="deposit",
            amount_tenths=150,
        )
        assert r1_dup.applied is False
        assert r1_dup.duplicate is True
        assert r1_dup.balance_after == 150

        # Withdraw -20.5 -> balance -5.5
        r2 = store.record_amount(
            owner_id=100,
            chat_id=2001,
            source_message_id=2,
            connection_id="conn1",
            kind="withdraw",
            amount_tenths=205,
        )
        assert r2.applied is True
        assert r2.balance_after == -55

        # Clear non-zero -> creates clear tx and pinned receipt
        r3 = store.record_clear(
            owner_id=100,
            chat_id=2001,
            source_message_id=3,
            connection_id="conn1",
        )
        assert r3.applied is True
        assert r3.balance_before == -55
        assert r3.balance_after == 0
        txs, total = store.get_recent_transactions(100, 2001, limit=10)
        assert total == 3
        assert [t.kind for t in txs] == ["clear", "withdraw", "deposit"]

        # Clear on brand-new customer with no wallet -> no wallet/tx created, unpinned receipt
        r4 = store.record_clear(
            owner_id=100,
            chat_id=9999,
            source_message_id=10,
            connection_id="conn1",
        )
        assert r4.applied is True
        assert store.get_customer(100, 9999) is None
        due = store.get_due_receipts()
        clear_zero_receipt = [x for x in due if x.chat_id == 9999][0]
        assert clear_zero_receipt.should_pin is False
    finally:
        store.close()


def test_admin_zero_optimistic_lock_and_lists_and_search(tmp_path: Path):
    store = Store(tmp_path / "jzbot.db")
    try:
        store.upsert_connection("c1", 1, True)
        store.record_amount(owner_id=1, chat_id=10, source_message_id=1, connection_id="c1", kind="deposit", amount_tenths=500, username="bob", first_name="Bob")
        store.record_amount(owner_id=1, chat_id=11, source_message_id=1, connection_id="c1", kind="withdraw", amount_tenths=300, first_name="Carol")
        store.record_amount(owner_id=1, chat_id=12, source_message_id=1, connection_id="c1", kind="deposit", amount_tenths=100, first_name="Dave")

        ov = store.get_owner_overview(1)
        assert ov.total_customers == 3
        assert ov.positive_count == 2
        assert ov.positive_sum_tenths == 600
        assert ov.debt_count == 1
        assert ov.debt_sum_tenths == -300

        # Positive sorted desc: Bob (500) before Dave (100)
        pos, _ = store.list_customers(1, "positive")
        assert [c.chat_id for c in pos] == [10, 12]

        # Search
        found, cnt = store.search_customers(1, "@bob")
        assert cnt == 1 and found[0].chat_id == 10
        assert store.search_customers(1, "@") == ([], 0)
        assert store.search_customers(1, "   ") == ([], 0)

        # Admin zero stale check
        assert store.admin_zero_customer(owner_id=1, chat_id=10, operator_id=1, expected_balance_tenths=400, expected_entry_count=1) == "stale"
        assert store.admin_zero_customer(owner_id=1, chat_id=10, operator_id=1, expected_balance_tenths=500, expected_entry_count=1) == "applied"
        assert store.admin_zero_customer(owner_id=1, chat_id=10, operator_id=1, expected_balance_tenths=0, expected_entry_count=2) == "noop"

        cust = store.get_customer(1, 10)
        assert cust.balance_tenths == 0
        assert cust.deposit_total_tenths == 500
        assert cust.entry_count == 2
    finally:
        store.close()


def test_broadcast_selection_and_startup_recovery(tmp_path: Path):
    store = Store(tmp_path / "jzbot.db")
    try:
        store.upsert_connection("c1", 1, True)
        store.record_amount(owner_id=1, chat_id=10, source_message_id=1, connection_id="c1", kind="deposit", amount_tenths=100)
        store.record_amount(owner_id=1, chat_id=11, source_message_id=1, connection_id="c1", kind="withdraw", amount_tenths=50)
        store.upsert_customer_profile(1, 12, "nowallet", "No", "Wallet")

        # Profile-only customer (no wallet) must NOT be selected
        recs = store.select_broadcast_recipients(1, {"non_negative"})
        assert [c.chat_id for c in recs] == [10]

        job_id = store.create_broadcast_job(1, ["non_negative", "debt"], "Hello all", [10, 11])
        claimed = store.claim_next_broadcast_recipient()
        assert claimed is not None
        assert claimed[1].status == "sending"

        # Simulate process restart -> running job paused, sending recipient marked uncertain
        store.recover_broadcasts_on_startup()
        active = store.get_active_broadcast(1)
        assert active is not None and active.status == "paused"
        counts = store.get_broadcast_counts(job_id)
        assert counts["uncertain"] == 1
        assert counts["pending"] == 1
    finally:
        store.close()


def test_hot_backup_and_receipt_retry(tmp_path: Path):
    store = Store(tmp_path / "jzbot.db")
    try:
        store.upsert_connection("c1", 1, True)
        res = store.record_amount(owner_id=1, chat_id=10, source_message_id=1, connection_id="c1", kind="deposit", amount_tenths=250)
        assert res.receipt_id is not None
        store.mark_receipt_retry(res.receipt_id, 300.0, "temp error")
        assert store.get_due_receipts() == []
        assert store.retry_all_pending_receipts(1) == 1
        assert len(store.get_due_receipts()) == 1

        bak_path = tmp_path / "backups" / "hot.db"
        store.backup_to(bak_path)
        bak_store = Store(bak_path)
        try:
            assert bak_store.integrity_check() == (True, 0)
            assert bak_store.get_customer(1, 10).balance_tenths == 250
        finally:
            bak_store.close()
    finally:
        store.close()
