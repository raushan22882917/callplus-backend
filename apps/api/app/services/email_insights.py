"""Opens, clicks and the best time to send, from the workspace's own events."""

import uuid
from datetime import UTC, datetime, timedelta

from app.services import send_time
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Tenant
from packages.database.models.email_event import EmailEvent

CLICK_WEIGHT = 3.0
WINDOW_DAYS = 90


async def engagement_by_campaign(db: AsyncSession, tenant_id: uuid.UUID, campaign_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict]:
    """People who opened and people who clicked (each counted once), and total clicks."""
    if not campaign_ids:
        return {}
    rows = (
        await db.execute(
            select(
                EmailEvent.campaign_id,
                func.count(func.distinct(case((EmailEvent.kind == "open", EmailEvent.customer_id)))),
                func.count(func.distinct(case((EmailEvent.kind == "click", EmailEvent.customer_id)))),
                func.count(case((EmailEvent.kind == "click", 1))),
            )
            .where(EmailEvent.tenant_id == tenant_id, EmailEvent.campaign_id.in_(campaign_ids))
            .group_by(EmailEvent.campaign_id)
        )
    ).all()
    return {row[0]: {"opened": int(row[1]), "clicked": int(row[2]), "clicks": int(row[3])} for row in rows}


async def top_links(db: AsyncSession, tenant_id: uuid.UUID, campaign_id: uuid.UUID | None = None, limit: int = 8) -> list[dict]:
    query = (
        select(EmailEvent.url, func.count(), func.count(func.distinct(EmailEvent.customer_id)))
        .where(EmailEvent.tenant_id == tenant_id, EmailEvent.kind == "click", EmailEvent.url.is_not(None))
        .group_by(EmailEvent.url)
        .order_by(func.count().desc())
        .limit(limit)
    )
    if campaign_id is not None:
        query = query.where(EmailEvent.campaign_id == campaign_id)
    return [{"url": url, "clicks": int(clicks), "people": int(people)} for url, clicks, people in (await db.execute(query)).all()]


async def engagement_grid(db: AsyncSession, tenant_id: uuid.UUID, tz_name: str, now: datetime | None = None) -> tuple[list[list[float]], int]:
    """Weekday-by-hour engagement, and how many opens and clicks it came from.

    Most opens come soon after a send, so raw counts mostly say "when we sent". Within one campaign each weekday and
    hour therefore counts as the square root of its events: a hundred opens in the hour after a send weigh about ten,
    and a pattern seen across several campaigns outweighs one burst.
    """
    tz = send_time.zone(tz_name)
    since = (now or datetime.now(UTC)) - timedelta(days=WINDOW_DAYS)
    rows = (
        await db.execute(
            select(EmailEvent.campaign_id, EmailEvent.created_at, EmailEvent.kind).where(
                EmailEvent.tenant_id == tenant_id, EmailEvent.created_at >= since
            )
        )
    ).all()
    cells: dict[tuple, float] = {}
    for campaign_id, created_at, kind in rows:
        local = (created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)).astimezone(tz)
        key = (campaign_id, local.weekday(), local.hour)
        cells[key] = cells.get(key, 0.0) + (CLICK_WEIGHT if kind == "click" else 1.0)
    grid = send_time.empty_grid()
    for (_, weekday, hour), weight in cells.items():
        grid[weekday][hour] += weight**0.5
    return grid, len(rows)


async def send_time_plan(db: AsyncSession, tenant_id: uuid.UUID, now: datetime | None = None, count: int = 3) -> dict:
    tenant = await db.get(Tenant, tenant_id)
    tz_name = (tenant.timezone if tenant else None) or "UTC"
    grid, events = await engagement_grid(db, tenant_id, tz_name, now)
    return {
        "timezone": tz_name,
        "slots": send_time.best_slots(grid, tz_name, now, count),
        "heatmap": [[round(v) for v in row] for row in grid],
        "events": events,
        "learned": round(min(1.0, sum(sum(row) for row in grid) / send_time.LEARNED_AT), 2),
    }
