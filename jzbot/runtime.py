"""One explicitly constructed runtime owns repositories, services and handlers."""

from .handlers.admin import AdminHandlers
from .handlers.broadcast import BroadcastHandlers
from .handlers.business import BusinessHandlers
from .handlers.receipts import ReceiptHandlers
from .repositories.store import LedgerStore
from .services.accounting import AccountingService
from .services.broadcast import BroadcastService
from .services.commands import CommandService
from .services.connections import ConnectionsService
from .services.management import ManagementService
from .services.outbox import OutboxService


class Runtime:
    def __init__(self, settings, *, store=None):
        self.settings = settings
        self.store = (
            store
            if store is not None
            else LedgerStore(
                settings.db_path, require_existing=settings.require_existing_db
            )
        )
        self.accounting = AccountingService(self.store)
        self.management = ManagementService(self.store)
        self.connections = ConnectionsService(self.store)
        self.outbox = OutboxService(self.store)
        self.commands = CommandService(self.accounting, self.outbox, settings.command_path)
        self.broadcast = BroadcastService(self.store, settings.broadcast_path)
        dependencies = (
            self.store,
            self.connections,
            self.outbox,
            self.accounting,
            self.management,
        )
        self.admin = AdminHandlers(*dependencies)
        self.admin.receipts = ReceiptHandlers(self.store)
        self.admin.broadcast = BroadcastHandlers(self.broadcast)
        self.business = BusinessHandlers(*dependencies)
        self.business.commands = self.commands

