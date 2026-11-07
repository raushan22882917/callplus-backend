import asyncio

from app.worker.celery_app import celery_app
from app.worker.tasks import ensure_engine
from sqlalchemy import select

from packages.database.models import Wallet
from packages.database.session import session_scope


async def _settle_all() -> int:
    from app.services.wallet import settle

    ensure_engine()
    async with session_scope() as db:
        tenant_ids = list((await db.execute(select(Wallet.tenant_id))).scalars().all())
    charged = 0
    for tenant_id in tenant_ids:
        async with session_scope() as db:
            charged += await settle(db, tenant_id)
            await db.commit()
    await _sync_overage()
    return charged


async def _sync_overage() -> None:
    """Refreshes the pending overage invoice for every workspace allowed past its plan."""
    from app.services.plans import sync_overage_invoice

    from packages.database.models import Tenant

    async with session_scope() as db:
        tenant_ids = list(
            (
                await db.execute(
                    select(Tenant.id).where(Tenant.plan_overrides["allow_overage"].astext == "true")
                )
            ).scalars().all()
        )
    for tenant_id in tenant_ids:
        async with session_scope() as db:
            await sync_overage_invoice(db, tenant_id)
            await db.commit()


@celery_app.task(name="wallet.settle_all")
def settle_all_task() -> int:
    """Periodic: charge every wallet for the calls and messages since the last pass, so the
    balance shown is current even when nobody is looking at it."""
    return asyncio.run(_settle_all())
