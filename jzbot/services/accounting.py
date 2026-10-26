"""Validate a trusted Business command before entering its atomic repository."""


class AccountingService:
    def __init__(self, store):
        self.store = store

    async def _authorize(self, owner, connection):
        info = await self.store.get_connection_owner(connection)
        if info != (owner, True):
            raise PermissionError(
                "An enabled connection belonging to this owner is required"
            )

    async def apply_amount(self, owner, customer, source, delta, connection):
        if type(delta) is not int or delta == 0:
            raise ValueError("Amount must be a nonzero integer number of tenths")
        await self._authorize(owner, connection)
        return await self.store.apply_amount(owner, customer, source, delta, connection)

    async def clear_balance(self, owner, customer, source, connection):
        await self._authorize(owner, connection)
        return await self.store.clear_balance(owner, customer, source, connection)
