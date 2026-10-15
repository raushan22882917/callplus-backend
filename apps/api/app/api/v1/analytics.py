from datetime import UTC, date, datetime, timedelta

from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.schemas.analytics import (
    AnalyticsOverview,
    CallsAnalytics,
    CampaignsAnalytics,
    DailyCount,
    TicketsAnalytics,
)
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Call,
    Campaign,
    CampaignContact,
    CampaignStatus,
    Customer,
    Ticket,
    TicketStatus,
)

router = APIRouter(prefix="/analytics", tags=["analytics"])


def _default_from_date() -> date:
    return (datetime.now(UTC) - timedelta(days=30)).date()


def _default_to_date() -> date:
    return datetime.now(UTC).date()


async def _count_by(db: AsyncSession, tenant_id, model, column) -> dict[str, int]:
    result = await db.execute(
        select(column, func.count()).where(model.tenant_id == tenant_id).group_by(column)
    )
    return {
        value.value if hasattr(value, "value") else value: count for value, count in result.all()
    }


@router.get("/overview", response_model=AnalyticsOverview)
async def get_overview(
    ctx: RequestContext = Depends(require_permission(Permissions.ANALYTICS_READ)),
    db: AsyncSession = Depends(get_db),
) -> AnalyticsOverview:
    tenant_id = ctx.tenant_id

    customers_count = (
        await db.execute(
            select(func.count()).select_from(Customer).where(Customer.tenant_id == tenant_id)
        )
    ).scalar_one()

    calls_by_status = await _count_by(db, tenant_id, Call, Call.status)
    calls_total = sum(calls_by_status.values())

    thirty_days_ago = datetime.now(UTC) - timedelta(days=30)
    calls_last_30_days = (
        await db.execute(
            select(func.count())
            .select_from(Call)
            .where(Call.tenant_id == tenant_id, Call.created_at >= thirty_days_ago)
        )
    ).scalar_one()

    tickets_by_status = await _count_by(db, tenant_id, Ticket, Ticket.status)
    tickets_total = sum(tickets_by_status.values())
    open_tickets = tickets_by_status.get(TicketStatus.OPEN.value, 0) + tickets_by_status.get(
        TicketStatus.PENDING.value, 0
    )

    active_campaigns = (
        await db.execute(
            select(func.count())
            .select_from(Campaign)
            .where(Campaign.tenant_id == tenant_id, Campaign.status == CampaignStatus.RUNNING)
        )
    ).scalar_one()

    return AnalyticsOverview(
        customers_count=customers_count,
        calls_total=calls_total,
        calls_by_status=calls_by_status,
        calls_last_30_days=calls_last_30_days,
        tickets_total=tickets_total,
        tickets_by_status=tickets_by_status,
        open_tickets=open_tickets,
        active_campaigns=active_campaigns,
    )


@router.get("/calls", response_model=CallsAnalytics)
async def get_calls_analytics(
    from_date: date = Query(default_factory=_default_from_date),
    to_date: date = Query(default_factory=_default_to_date),
    ctx: RequestContext = Depends(require_permission(Permissions.ANALYTICS_READ)),
    db: AsyncSession = Depends(get_db),
) -> CallsAnalytics:
    tenant_id = ctx.tenant_id
    range_filter = (
        func.date(Call.created_at) >= from_date,
        func.date(Call.created_at) <= to_date,
    )

    by_status = await _count_by(db, tenant_id, Call, Call.status)
    by_direction = await _count_by(db, tenant_id, Call, Call.direction)

    total_duration = (
        await db.execute(
            select(func.coalesce(func.sum(Call.duration_seconds), 0)).where(
                Call.tenant_id == tenant_id
            )
        )
    ).scalar_one()

    daily = await db.execute(
        select(func.date(Call.created_at).label("day"), func.count())
        .where(Call.tenant_id == tenant_id, *range_filter)
        .group_by("day")
        .order_by("day")
    )

    return CallsAnalytics(
        by_status=by_status,
        by_direction=by_direction,
        total_duration_seconds=total_duration,
        daily_volume=[DailyCount(day=day, count=count) for day, count in daily.all()],
    )


@router.get("/tickets", response_model=TicketsAnalytics)
async def get_tickets_analytics(
    from_date: date = Query(default_factory=_default_from_date),
    to_date: date = Query(default_factory=_default_to_date),
    ctx: RequestContext = Depends(require_permission(Permissions.ANALYTICS_READ)),
    db: AsyncSession = Depends(get_db),
) -> TicketsAnalytics:
    tenant_id = ctx.tenant_id

    by_status = await _count_by(db, tenant_id, Ticket, Ticket.status)
    by_priority = await _count_by(db, tenant_id, Ticket, Ticket.priority)

    daily = await db.execute(
        select(func.date(Ticket.created_at).label("day"), func.count())
        .where(
            Ticket.tenant_id == tenant_id,
            func.date(Ticket.created_at) >= from_date,
            func.date(Ticket.created_at) <= to_date,
        )
        .group_by("day")
        .order_by("day")
    )

    return TicketsAnalytics(
        by_status=by_status,
        by_priority=by_priority,
        daily_created=[DailyCount(day=day, count=count) for day, count in daily.all()],
    )


@router.get("/campaigns", response_model=CampaignsAnalytics)
async def get_campaigns_analytics(
    ctx: RequestContext = Depends(require_permission(Permissions.ANALYTICS_READ)),
    db: AsyncSession = Depends(get_db),
) -> CampaignsAnalytics:
    tenant_id = ctx.tenant_id

    by_status = await _count_by(db, tenant_id, Campaign, Campaign.status)
    contacts_by_status = await _count_by(db, tenant_id, CampaignContact, CampaignContact.status)

    return CampaignsAnalytics(by_status=by_status, contacts_by_status=contacts_by_status)
