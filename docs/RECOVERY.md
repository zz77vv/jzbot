# Trusted command recovery

Authenticated Business owner amount commands and `//` are saved to `commands.db`
beside the configured log before accounting runs. This is independent runtime
data: no recovery tables or schema changes are added to the financial ledger.
The sidecar records the canonical ledger path and refuses to open for another
ledger. Unrelated databases and symlinks are rejected. Keep the queue with its
original ledger and runtime directory; changing that path requires an explicit
operator migration. Replacing ledger contents at the same path also requires
reconciling the associated queue first.

Normal commands are attempted immediately. The existing accounting transaction
still commits the wallet, history, statistics, source deduplication and original
receipt together. Customer replies and pins remain the receipt Outbox worker's
responsibility. `/` remains the existing best-effort, read-only recent query; it
shows committed entries and does not wait for pending mutations.

Each queue item preserves `(owner, customer, source message)` and its first
accepted payload. Duplicate delivery cannot replace the amount, reset its retry
deadline or create another item. A ledger commit followed by a failed queue
acknowledgement is ambiguous, so replay uses that original source key. The
ledger's existing atomic deduplication confirms the transaction without adding
the amount again. The service never compensates or claims a rollback after an
uncertain acknowledgement.

Pending mutations are processed in accepted order for each owner/customer pair.
An earlier failed amount blocks a later `//` for that customer; other customers
and owners can continue. Current ownership and connection enablement are checked
again on every accounting attempt. A disabled or reassigned original connection
keeps the command pending rather than using another owner's connection.

Failures retry indefinitely with delays of 2, 4, 8, …, 256, then 300 seconds,
plus worker scheduling time. Persistent failures stay in the queue. The owner
receives a private deferred notice identifying the customer and original message,
and a recovery confirmation after accounting is confirmed. Deferred notices are
not repeated after their delivery is acknowledged. Failed notice delivery is
retained and retried after 30 seconds. Telegram delivery/acknowledgement ambiguity
can duplicate a private notice, but cannot duplicate a financial transaction.
Notices run in an independent worker with a separate lock and a 15-second send
timeout. Accounting only saves notice intent: a slow owner's private notice
cannot hold the accounting lock or delay another owner's healthy command. If a
deferred notice is already in flight when accounting succeeds, its eventual
acknowledgement cannot erase the newer recovery confirmation.

If the sidecar cannot save an incoming command, accounting is not attempted.
The service tries a private warning that processing is unconfirmed and asks the
owner to inspect the original message and ledger, avoiding a new same-amount
command. A total queue storage failure cannot durably retain an unsaved command
or its warning; if Telegram is unavailable too, only the runtime log can report
it. Restore storage, inspect the queue and original source, and reconcile before
issuing replacements. Runtime storage and Telegram availability remain necessary;
the queue is not a backup of the ledger.

Startup opens and validates the sidecar, then starts the trusted-command worker.
Pending records survive process restart without an in-progress lease to recover.
Shutdown cancels and awaits this worker before closing the financial store.
The command worker also cancels and awaits its private-notice child on shutdown,
including while a notice send is blocked.
Cancellation before a ledger commit rolls back the accounting transaction;
cancellation after a commit replays safely using the original deduplication key.
Each sidecar operation closes its connection, so importing or merely constructing
a Runtime opens no database or other runtime files.

Admin reconciliation acts on the committed ledger snapshot. Resolve deferred
commands before manual admin adjustments or zeroing balances; pending mutations
otherwise still apply later in their original accepted order. Inspect
`trusted_commands` in the separate sidecar (`pending`/`done`, original source,
attempts, next due time and exception type) and the owner notices when diagnosing
an incident. Do not delete pending records or rename the sidecar to bypass a
startup identity check.

Offline regression coverage is in `tests/test_command_recovery.py`: rollback and
retry, persistent failure and notices, restart, amount/clear ordering, duplicate
source and payload, acknowledgement loss, owner/customer isolation and revoked
authorization, persistence failure, failed notices, large integer tenths, worker
cancellation/shutdown/restart, sidecar separation and ledger identity checks.
Notice tests also cover blocked-owner isolation, bounded sends, notice-task
cleanup and deferred/recovered acknowledgement races.
