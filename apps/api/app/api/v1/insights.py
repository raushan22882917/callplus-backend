import uuid
from datetime import UTC, datetime, timedelta

from app.core.deps import RequestContext, get_db, require_permission
from app.core.exceptions import NotFoundError
from app.core.permissions import Permissions
from app.core.repository import TenantScopedRepository
from app.schemas.insights import (
    CopilotSuggestion,
    CopilotSuggestRequest,
    CopilotSummarizeRequest,
    CopilotSummary,
    QAScoreListItem,
    QAScoreRead,
    QASummary,
    RescoreRequest,
    ScorecardCreate,
    ScorecardRead,
    ScorecardUpdate,
)
from app.services import copilot, cx_analytics, forecast, ops_analytics, qa, transcript_search
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import AIAgent, Call, CallStatus, Customer
from packages.database.models.insights import CallQAScore, QAScorecard

router = APIRouter(prefix="/insights", tags=["insights"])

CALLS_READ = Depends(require_permission(Permissions.CALLS_READ))
CALLS_MANAGE = Depends(require_permission(Permissions.CALLS_MANAGE))
ANALYTICS_READ = Depends(require_permission(Permissions.ANALYTICS_READ))
SETTINGS_MANAGE = Depends(require_permission(Permissions.SETTINGS_MANAGE))


class ScorecardRepository(TenantScopedRepository[QAScorecard]):
    model = QAScorecard


def _clean_list(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    return [v.strip()[:300] for v in values if v and v.strip()]


# ---------------------------------------------------------------- QA


@router.get("/qa/summary", response_model=QASummary)
async def qa_summary(
    days: int = Query(default=30, ge=1, le=365),
    ctx: RequestContext = CALLS_READ,
    db: AsyncSession = Depends(get_db),
) -> QASummary:
    since = datetime.now(UTC) - timedelta(days=days)
    row = (
        await db.execute(
            select(
                func.count(CallQAScore.id),
                func.avg(CallQAScore.overall),
                func.avg(CallQAScore.compliance),
                func.avg(CallQAScore.communication),
                func.avg(CallQAScore.resolution),
                func.avg(CallQAScore.sales),
            ).where(CallQAScore.tenant_id == ctx.tenant_id, CallQAScore.scored_at >= since)
        )
    ).one()
    scores = (
        await db.execute(
            select(CallQAScore.problems).where(
                CallQAScore.tenant_id == ctx.tenant_id, CallQAScore.scored_at >= since
            )
        )
    ).scalars()
    critical = sum(1 for problems in scores if any(p.get("severity") == "critical" for p in problems))

    def r(v):
        return round(float(v), 1) if v is not None else None

    return QASummary(
        scored_calls=row[0],
        avg_overall=r(row[1]),
        avg_compliance=r(row[2]),
        avg_communication=r(row[3]),
        avg_resolution=r(row[4]),
        avg_sales=r(row[5]),
        calls_with_critical_problems=critical,
    )


@router.get("/qa/scores", response_model=list[QAScoreListItem])
async def list_qa_scores(
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    max_overall: int | None = Query(default=None, ge=0, le=100),
    min_overall: int | None = Query(default=None, ge=0, le=100),
    ctx: RequestContext = CALLS_READ,
    db: AsyncSession = Depends(get_db),
) -> list[QAScoreListItem]:
    stmt = (
        select(CallQAScore, Call, Customer.name, AIAgent.name)
        .join(Call, Call.id == CallQAScore.call_id)
        .outerjoin(Customer, Customer.id == Call.customer_id)
        .outerjoin(AIAgent, AIAgent.id == Call.ai_agent_id)
        .where(CallQAScore.tenant_id == ctx.tenant_id)
        .order_by(CallQAScore.scored_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if max_overall is not None:
        stmt = stmt.where(CallQAScore.overall <= max_overall)
    if min_overall is not None:
        stmt = stmt.where(CallQAScore.overall >= min_overall)
    items = []
    for score, call, customer_name, agent_name in (await db.execute(stmt)).all():
        base = QAScoreRead.model_validate(score).model_dump()
        items.append(
            QAScoreListItem(
                **base,
                call_created_at=call.created_at,
                direction=call.direction.value,
                customer_name=customer_name,
                agent_name=agent_name,
                duration_seconds=call.duration_seconds,
            )
        )
    return items


@router.get("/qa/scores/{call_id}", response_model=QAScoreRead)
async def get_qa_score(
    call_id: uuid.UUID,
    ctx: RequestContext = CALLS_READ,
    db: AsyncSession = Depends(get_db),
) -> CallQAScore:
    row = (
        await db.execute(
            select(CallQAScore).where(
                CallQAScore.tenant_id == ctx.tenant_id, CallQAScore.call_id == call_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("This call has not been QA-scored")
    return row


@router.post("/qa/scores/{call_id}/rescore", response_model=QAScoreRead)
async def rescore_call(
    call_id: uuid.UUID,
    payload: RescoreRequest | None = None,
    ctx: RequestContext = CALLS_MANAGE,
    db: AsyncSession = Depends(get_db),
) -> CallQAScore:
    call = await db.get(Call, call_id)
    if call is None or call.tenant_id != ctx.tenant_id:
        raise NotFoundError("Call not found")
    scorecard_id = payload.scorecard_id if payload else None
    if scorecard_id is not None:
        await ScorecardRepository(db, ctx.tenant_id).get(scorecard_id)
    row = await qa.score_call(db, call_id, scorecard_id)
    if row is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Nothing to score: the call needs both agent and caller turns in its transcript",
        )
    return row


@router.get("/qa/scorecards", response_model=list[ScorecardRead])
async def list_scorecards(
    ctx: RequestContext = CALLS_READ, db: AsyncSession = Depends(get_db)
) -> list[QAScorecard]:
    return await ScorecardRepository(db, ctx.tenant_id).list(limit=200)


async def _clear_default(db: AsyncSession, tenant_id: uuid.UUID, keep: uuid.UUID | None) -> None:
    stmt = update(QAScorecard).where(QAScorecard.tenant_id == tenant_id).values(is_default=False)
    if keep is not None:
        stmt = stmt.where(QAScorecard.id != keep)
    await db.execute(stmt)


@router.post("/qa/scorecards", response_model=ScorecardRead, status_code=status.HTTP_201_CREATED)
async def create_scorecard(
    payload: ScorecardCreate,
    ctx: RequestContext = SETTINGS_MANAGE,
    db: AsyncSession = Depends(get_db),
) -> QAScorecard:
    data = payload.model_dump()
    for key in ("greeting_phrases", "script_phrases", "forbidden_claims", "closing_phrases"):
        data[key] = _clean_list(data[key])
    card = await ScorecardRepository(db, ctx.tenant_id).create(**data)
    if card.is_default:
        await _clear_default(db, ctx.tenant_id, card.id)
    await db.commit()
    await db.refresh(card)
    return card


@router.patch("/qa/scorecards/{scorecard_id}", response_model=ScorecardRead)
async def update_scorecard(
    scorecard_id: uuid.UUID,
    payload: ScorecardUpdate,
    ctx: RequestContext = SETTINGS_MANAGE,
    db: AsyncSession = Depends(get_db),
) -> QAScorecard:
    card = await ScorecardRepository(db, ctx.tenant_id).get(scorecard_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        if field in ("greeting_phrases", "script_phrases", "forbidden_claims", "closing_phrases"):
            value = _clean_list(value) or []
        if value is None and field in ("name", "is_default", "introduction_required", "max_silence_seconds"):
            continue
        setattr(card, field, value)
    if card.is_default:
        await _clear_default(db, ctx.tenant_id, card.id)
    await db.commit()
    await db.refresh(card)
    return card


@router.delete("/qa/scorecards/{scorecard_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scorecard(
    scorecard_id: uuid.UUID,
    ctx: RequestContext = SETTINGS_MANAGE,
    db: AsyncSession = Depends(get_db),
) -> None:
    await ScorecardRepository(db, ctx.tenant_id).delete(scorecard_id)
    await db.commit()


# ---------------------------------------------------------------- Copilot


def _turns(items) -> list[qa.Turn]:
    return [qa.Turn(speaker=t.speaker, text=t.text) for t in items]


async def _assert_call(db: AsyncSession, ctx: RequestContext, call_id: uuid.UUID | None) -> None:
    if call_id is None:
        return
    call = await db.get(Call, call_id)
    if call is None or call.tenant_id != ctx.tenant_id:
        raise NotFoundError("Call not found")


@router.post("/copilot/suggest", response_model=CopilotSuggestion)
async def copilot_suggest(
    payload: CopilotSuggestRequest,
    ctx: RequestContext = CALLS_READ,
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _assert_call(db, ctx, payload.call_id)
    try:
        return await copilot.suggest(
            db,
            tenant_id=ctx.tenant_id,
            caller_text=payload.caller_text,
            call_id=payload.call_id,
            conversation=_turns(payload.conversation),
        )
    except copilot.CopilotError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.post("/copilot/summarize", response_model=CopilotSummary)
async def copilot_summarize(
    payload: CopilotSummarizeRequest,
    ctx: RequestContext = CALLS_READ,
    db: AsyncSession = Depends(get_db),
) -> dict:
    if payload.call_id is None and not payload.conversation:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Provide call_id or conversation")
    await _assert_call(db, ctx, payload.call_id)
    try:
        return await copilot.summarize(
            db,
            tenant_id=ctx.tenant_id,
            call_id=payload.call_id,
            conversation=_turns(payload.conversation),
        )
    except copilot.CopilotError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


# ---------------------------------------------------------------- Customer experience


@router.get("/cx/sentiment-trend")
async def cx_sentiment_trend(
    days: int = Query(default=30, ge=1, le=365),
    ctx: RequestContext = ANALYTICS_READ,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    return await cx_analytics.sentiment_trend(db, ctx.tenant_id, days)


@router.get("/cx/intents")
async def cx_intents(
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=15, ge=1, le=50),
    ctx: RequestContext = ANALYTICS_READ,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    return await cx_analytics.intent_distribution(db, ctx.tenant_id, days, limit)


@router.get("/cx/complaints")
async def cx_complaints(
    days: int = Query(default=30, ge=1, le=365),
    ctx: RequestContext = ANALYTICS_READ,
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await cx_analytics.complaints(db, ctx.tenant_id, days)


@router.get("/cx/customer-scores")
async def cx_customer_scores(
    sort: str = Query(default="churn", pattern="^(churn|buying)$"),
    limit: int = Query(default=50, ge=1, le=200),
    ctx: RequestContext = ANALYTICS_READ,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    return await cx_analytics.customer_scores(db, ctx.tenant_id, sort=sort, limit=limit)


@router.get("/cx/customers/{customer_id}/scores")
async def cx_single_customer_scores(
    customer_id: uuid.UUID,
    ctx: RequestContext = ANALYTICS_READ,
    db: AsyncSession = Depends(get_db),
) -> dict:
    customer = await db.get(Customer, customer_id)
    if customer is None or customer.tenant_id != ctx.tenant_id:
        raise NotFoundError("Customer not found")
    return await cx_analytics.single_customer_scores(db, ctx.tenant_id, customer_id)


@router.get("/cx/customers/{customer_id}/journey")
async def cx_journey(
    customer_id: uuid.UUID,
    ctx: RequestContext = ANALYTICS_READ,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    customer = await db.get(Customer, customer_id)
    if customer is None or customer.tenant_id != ctx.tenant_id:
        raise NotFoundError("Customer not found")
    return await cx_analytics.journey(db, ctx.tenant_id, customer_id)


@router.get("/cx/repeat-callers")
async def cx_repeat_callers(
    days: int = Query(default=30, ge=1, le=365),
    min_calls: int = Query(default=2, ge=2, le=50),
    ctx: RequestContext = ANALYTICS_READ,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    return await cx_analytics.repeat_callers(db, ctx.tenant_id, days, min_calls)


@router.get("/cx/customer-value")
async def cx_customer_value(
    limit: int = Query(default=50, ge=1, le=200),
    ctx: RequestContext = ANALYTICS_READ,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    return await cx_analytics.customer_value(db, ctx.tenant_id, limit)


# ---------------------------------------------------------------- Advanced analytics


@router.get("/analytics/agents")
async def analytics_agents(
    days: int = Query(default=30, ge=1, le=365),
    ctx: RequestContext = ANALYTICS_READ,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    return await ops_analytics.agent_analytics(db, ctx.tenant_id, days)


@router.get("/analytics/human-agents")
async def analytics_human_agents(
    days: int = Query(default=30, ge=1, le=365),
    ctx: RequestContext = ANALYTICS_READ,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    return await ops_analytics.human_agent_analytics(db, ctx.tenant_id, days)


@router.get("/analytics/campaigns")
async def analytics_campaigns(
    ctx: RequestContext = ANALYTICS_READ, db: AsyncSession = Depends(get_db)
) -> list[dict]:
    return await ops_analytics.campaign_analytics(db, ctx.tenant_id)


@router.get("/analytics/ai")
async def analytics_ai(
    days: int = Query(default=30, ge=1, le=365),
    ctx: RequestContext = ANALYTICS_READ,
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await ops_analytics.ai_analytics(db, ctx.tenant_id, days)


@router.get("/analytics/forecast")
async def analytics_forecast(
    days: int = Query(default=7, ge=1, le=14),
    window_weeks: int = Query(default=4, ge=1, le=12),
    tz_offset_minutes: int = Query(default=0, ge=-840, le=840),
    aht_seconds: int | None = Query(default=None, ge=10, le=7200),
    target_service_level: float = Query(default=0.8, gt=0, lt=1),
    answer_seconds: int = Query(default=20, ge=1, le=600),
    max_occupancy: float = Query(default=0.85, gt=0, le=1),
    ctx: RequestContext = ANALYTICS_READ,
    db: AsyncSession = Depends(get_db),
) -> dict:
    now = datetime.now(UTC)
    since = now - timedelta(weeks=window_weeks)
    times = list(
        (
            await db.execute(
                select(Call.created_at).where(Call.tenant_id == ctx.tenant_id, Call.created_at >= since)
            )
        ).scalars()
    )
    measured_aht = (
        await db.execute(
            select(func.avg(Call.duration_seconds)).where(
                Call.tenant_id == ctx.tenant_id,
                Call.status == CallStatus.COMPLETED,
                Call.duration_seconds > 0,
                Call.created_at >= since,
            )
        )
    ).scalar_one()
    aht = aht_seconds or (round(float(measured_aht)) if measured_aht else None)
    fc = forecast.forecast_days(
        times, now=now, days=days, window_weeks=window_weeks, tz_offset_minutes=tz_offset_minutes
    )
    plan = (
        forecast.staffing_plan(
            fc,
            aht,
            target_service_level=target_service_level,
            answer_seconds=answer_seconds,
            max_occupancy=max_occupancy,
        )
        if aht
        else None
    )
    return {
        "window_weeks": window_weeks,
        "history_calls": len(times),
        "aht_seconds": aht,
        "aht_source": "override" if aht_seconds else ("measured" if aht else None),
        "assumptions": {
            "target_service_level": target_service_level,
            "answer_seconds": answer_seconds,
            "max_occupancy": max_occupancy,
            "method": "Moving average per weekday-hour; Erlang C staffing",
        },
        "forecast": fc,
        "staffing": plan,
    }


@router.get("/search")
async def search_transcripts(
    q: str = Query(min_length=2, max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
    ctx: RequestContext = CALLS_READ,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    return await transcript_search.search_transcripts(db, ctx.tenant_id, q, limit=limit)
