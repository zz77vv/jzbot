"""V1-compatible schema. No ledger ALTER, renumbering or replacement."""

BASE_SCHEMA = """
            CREATE TABLE IF NOT EXISTS business_accounts (
                owner_user_id       INTEGER PRIMARY KEY,
                welcome_sent        INTEGER NOT NULL DEFAULT 0,
                first_seen_at       TEXT NOT NULL,
                last_seen_at        TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS business_connections (
                connection_id       TEXT PRIMARY KEY,
                owner_user_id       INTEGER NOT NULL,
                is_enabled          INTEGER NOT NULL,
                updated_at          TEXT NOT NULL,
                FOREIGN KEY(owner_user_id)
                    REFERENCES business_accounts(owner_user_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_business_connections_owner
                ON business_connections(owner_user_id);

            CREATE TABLE IF NOT EXISTS customer_profiles (
                owner_user_id       INTEGER NOT NULL,
                customer_chat_id    INTEGER NOT NULL,
                username            TEXT,
                first_name          TEXT,
                last_name           TEXT,
                updated_at          TEXT NOT NULL,
                PRIMARY KEY(owner_user_id, customer_chat_id),
                FOREIGN KEY(owner_user_id)
                    REFERENCES business_accounts(owner_user_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS wallets (
                owner_user_id       INTEGER NOT NULL,
                customer_chat_id    INTEGER NOT NULL,
                balance_tenths      TEXT NOT NULL DEFAULT '0',
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL,
                PRIMARY KEY(owner_user_id, customer_chat_id),
                FOREIGN KEY(owner_user_id)
                    REFERENCES business_accounts(owner_user_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_wallets_owner_updated
                ON wallets(owner_user_id, updated_at DESC);

            CREATE TABLE IF NOT EXISTS ledger (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id       INTEGER NOT NULL,
                customer_chat_id    INTEGER NOT NULL,
                source_message_id   INTEGER NOT NULL,
                entry_type          TEXT NOT NULL CHECK(entry_type IN ('deposit','withdraw','clear')),
                amount_tenths       TEXT NOT NULL,
                balance_before      TEXT NOT NULL,
                balance_after       TEXT NOT NULL,
                created_at_utc      TEXT NOT NULL,
                UNIQUE(owner_user_id, customer_chat_id, source_message_id),
                FOREIGN KEY(owner_user_id)
                    REFERENCES business_accounts(owner_user_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_ledger_recent
                ON ledger(owner_user_id, customer_chat_id, id DESC);

            CREATE TABLE IF NOT EXISTS processed_messages (
                owner_user_id       INTEGER NOT NULL,
                customer_chat_id    INTEGER NOT NULL,
                source_message_id   INTEGER NOT NULL,
                command_type        TEXT NOT NULL,
                processed_at_utc    TEXT NOT NULL,
                PRIMARY KEY(owner_user_id, customer_chat_id, source_message_id)
            );

            -- 客户累计统计。把原来“点客户时扫描全部历史流水”的工作，
            -- 改为记账时顺手维护，后台详情只需读取一行。
            CREATE TABLE IF NOT EXISTS wallet_stats (
                owner_user_id           INTEGER NOT NULL,
                customer_chat_id        INTEGER NOT NULL,
                deposit_total_tenths    TEXT NOT NULL DEFAULT '0',
                withdraw_total_tenths   TEXT NOT NULL DEFAULT '0',
                entry_count             INTEGER NOT NULL DEFAULT 0,
                updated_at              TEXT NOT NULL,
                PRIMARY KEY(owner_user_id, customer_chat_id),
                FOREIGN KEY(owner_user_id)
                    REFERENCES business_accounts(owner_user_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS receipt_outbox (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id           INTEGER NOT NULL,
                customer_chat_id        INTEGER NOT NULL,
                source_message_id       INTEGER NOT NULL,
                business_connection_id  TEXT NOT NULL,
                receipt_type            TEXT NOT NULL
                                        CHECK(receipt_type IN ('amount','clear')),
                receipt_text            TEXT NOT NULL,
                reply_to_message_id     INTEGER,
                should_pin              INTEGER NOT NULL DEFAULT 1,
                status                  TEXT NOT NULL DEFAULT 'pending'
                                        CHECK(status IN ('pending','delivered')),
                attempt_count           INTEGER NOT NULL DEFAULT 0,
                created_at_utc          TEXT NOT NULL,
                next_attempt_at_utc     TEXT NOT NULL,
                delivered_at_utc        TEXT,
                last_error              TEXT,
                UNIQUE(
                    owner_user_id,
                    customer_chat_id,
                    source_message_id,
                    receipt_type
                ),
                FOREIGN KEY(owner_user_id)
                    REFERENCES business_accounts(owner_user_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_receipt_outbox_due
                ON receipt_outbox(status, next_attempt_at_utc, id);

            CREATE TABLE IF NOT EXISTS app_meta (
                meta_key     TEXT PRIMARY KEY,
                meta_value   TEXT NOT NULL
            );
            """
MANAGEMENT_SQL = (
    """
                    CREATE TABLE IF NOT EXISTS customer_management (
                        owner_user_id INTEGER NOT NULL,
                        customer_chat_id INTEGER NOT NULL,
                        is_disabled INTEGER NOT NULL CHECK(is_disabled IN (0,1)),
                        updated_by INTEGER NOT NULL,
                        updated_at_utc TEXT NOT NULL,
                        PRIMARY KEY(owner_user_id, customer_chat_id)
                    )
                """,
    """
                    CREATE TABLE IF NOT EXISTS admin_operations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        operation_id TEXT NOT NULL UNIQUE,
                        owner_user_id INTEGER NOT NULL,
                        administrator_id INTEGER NOT NULL,
                        customer_chat_id INTEGER NOT NULL,
                        operation_type TEXT NOT NULL DEFAULT 'admin_zero'
                            CHECK(operation_type = 'admin_zero'),
                        entry_type TEXT NOT NULL DEFAULT 'clear' CHECK(entry_type = 'clear'),
                        expected_balance TEXT NOT NULL,
                        expected_ledger_id INTEGER NOT NULL,
                        expected_admin_id INTEGER NOT NULL,
                        expected_entry_count INTEGER NOT NULL,
                        balance_before TEXT,
                        balance_after TEXT,
                        result TEXT NOT NULL DEFAULT 'pending' CHECK(result IN
                            ('pending','applied','noop','cancelled','expired','stale','failed')),
                        created_at_utc TEXT NOT NULL,
                        expires_at_utc TEXT NOT NULL,
                        completed_at_utc TEXT,
                        ledger_anchor_id INTEGER,
                        back_kind TEXT NOT NULL CHECK(back_kind IN ('a','p','d','s')),
                        back_page INTEGER NOT NULL CHECK(back_page >= 0)
                    )
                """,
    """
                    CREATE INDEX IF NOT EXISTS idx_admin_clear_history
                    ON admin_operations(owner_user_id, customer_chat_id,
                                        ledger_anchor_id DESC, id DESC)
                    WHERE result = 'applied'
                """,
)
SCHEMA_VERSION = "1"
