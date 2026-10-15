"""Cross-tenant uniqueness checks ("is this Page / phone number already
connected to another workspace?").

They must see other tenants' rows, which row-level security hides from a
tenant-scoped session, so they run in a short tenant-less session. They only
return a boolean: nothing about the other tenant leaks."""

import uuid
from typing import Any

from sqlalchemy import select

from packages.database.session import session_scope


async def other_tenant_owns(model: Any, tenant_id: uuid.UUID, **filters: Any) -> bool:
    query = select(model.id).where(
        model.tenant_id != tenant_id, *[getattr(model, k) == v for k, v in filters.items()]
    )
    async with session_scope() as session:
        return (await session.execute(query.limit(1))).first() is not None
