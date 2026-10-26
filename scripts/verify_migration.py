"""Migrate an explicitly supplied rehearsal DB, compare every row/scope, repeat.

Never point this tool at a live ledger. Real-data rehearsals run only in a protected
server recovery directory after polling has stopped; ordinary tests use fixtures.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jzbot.db.reconciliation import assert_preserved, snapshot
from jzbot.repositories.store import LedgerStore


async def rehearse(path):
    before = snapshot(path)
    for _ in range(2):
        store = LedgerStore(path, require_existing=True)
        await store.open()
        try:
            rows = await (
                await store.conn.execute(
                    "SELECT owner_user_id,customer_chat_id FROM wallets"
                )
            ).fetchall()
            for owner, customer in rows:
                detail = await store.get_customer_detail(owner, customer)
                assert detail is not None
                entries, total, _ = await store.get_ledger_page(owner, customer)
                assert len(entries) == min(total, 10)
        finally:
            await store.close()
        assert_preserved(before, snapshot(path))
    return before.public_summary()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rehearsal-db", type=Path, required=True)
    args = parser.parse_args()
    path = args.rehearsal_db.resolve(strict=True)
    if path == Path("/root/jzbot/ledger.db") or path.name == "ledger.rollback.db":
        parser.error(
            "Refusing live ledger or the immutable rollback point; supply a rehearsal copy"
        )
    print("RECONCILIATION_OK", asyncio.run(rehearse(path)))


if __name__ == "__main__":
    main()
