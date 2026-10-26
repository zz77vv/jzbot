"""Unit-of-work facade over cohesive SQL repositories; no module globals.

Cross-repository writes use this same connection/lock, so accounting, statistics,
deduplication and durable receipts never escape their common transaction.
"""

from jzbot.db.database import Database

from .accounting import AccountingRepository
from .accounts import AccountsRepository
from .customers import CustomersRepository
from .history import HistoryRepository
from .management import ManagementRepository
from .outbox import OutboxRepository
from .stats import StatsRepository


class LedgerStore:
    COMPONENTS = (
        AccountsRepository,
        AccountingRepository,
        CustomersRepository,
        HistoryRepository,
        ManagementRepository,
        OutboxRepository,
        StatsRepository,
    )

    def __init__(self, path, *, require_existing=False):
        self.database = Database(path, require_existing=require_existing)
        self._methods = {}
        self._repositories = []
        for component in self.COMPONENTS:
            repository = component(self)
            self._repositories.append(repository)
            for name, member in vars(component).items():
                if callable(member) and name != "__init__":
                    self._methods[name] = repository

    @property
    def db_path(self):
        return self.database.db_path

    @property
    def conn(self):
        return self.database.conn

    @conn.setter
    def conn(self, value):
        self.database.conn = value

    @property
    def write_lock(self):
        return self.database.write_lock

    @property
    def delivery_lock(self):
        return self.database.delivery_lock

    def __getattr__(self, name):
        repository = self._methods.get(name)
        if repository is not None:
            return getattr(repository, name)
        if name in {
            "open",
            "close",
            "_require_conn",
            "_ensure_customer_management_schema",
        }:
            return getattr(self.database, name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        # Supports per-unit-of-work fault injection without changing global classes.
        repository = vars(self).get("_methods", {}).get(name)
        if repository is not None:
            setattr(repository, name, value)
        else:
            object.__setattr__(self, name, value)
