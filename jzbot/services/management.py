"""Owner authorization independent of mutable page/search state."""


class ManagementService:
    def __init__(self, store):
        self.store = store

    async def _authorize(self, owner, actor):
        if owner != actor or not await self.store.is_business_owner(owner):
            raise PermissionError("Business owner required")

    async def create_admin_zero(self, owner, actor, customer, *args, **kwargs):
        await self._authorize(owner, actor)
        request = await self.store.create_admin_zero(
            owner, actor, customer, *args, **kwargs
        )
        if args and args[0] == "i":
            # Persist only V1-compatible back_kind values; trusted UI state is ephemeral.
            request["ui_back_kind"] = "i"
        return request

    async def resolve_admin_zero(self, owner, actor, request, **kwargs):
        await self._authorize(owner, actor)
        return await self.store.resolve_admin_zero(owner, actor, request, **kwargs)

    async def set_customer_disabled(self, owner, actor, customer, disabled):
        await self._authorize(owner, actor)
        return await self.store.set_customer_disabled(owner, actor, customer, disabled)
