import uuid

from app.core.exceptions import NotFoundError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.mixins import TenantScopedMixin, UUIDPrimaryKeyMixin


class TenantScopedRepository[ModelT: UUIDPrimaryKeyMixin]:
    """CRUD helper that hard-codes tenant_id into every query.

    tenant_id always comes from the caller's RequestContext (see
    app/core/deps.py), never from a path/query/body parameter — this is
    what makes cross-tenant data access structurally impossible here rather
    than dependent on every route remembering to filter.
    """

    model: type[ModelT]

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID):
        assert issubclass(self.model, TenantScopedMixin)
        self.db = db
        self.tenant_id = tenant_id

    async def get(self, obj_id: uuid.UUID) -> ModelT:
        result = await self.db.execute(
            select(self.model).where(
                self.model.id == obj_id, self.model.tenant_id == self.tenant_id
            )
        )
        obj = result.scalar_one_or_none()
        if obj is None:
            raise NotFoundError(f"{self.model.__name__} not found")
        return obj

    async def list(self, *, limit: int = 50, offset: int = 0) -> list[ModelT]:
        result = await self.db.execute(
            select(self.model)
            .where(self.model.tenant_id == self.tenant_id)
            .order_by(self.model.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def create(self, **fields) -> ModelT:
        obj = self.model(tenant_id=self.tenant_id, **fields)
        self.db.add(obj)
        await self.db.flush()
        return obj

    async def delete(self, obj_id: uuid.UUID) -> None:
        obj = await self.get(obj_id)
        await self.db.delete(obj)
