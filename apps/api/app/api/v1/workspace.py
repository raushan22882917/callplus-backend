"""Human agent workspace: agent presence/profiles, routing configuration
(business hours, holidays), the call handoff package, live-call control via
Twilio Conference, and the supervisor live board."""

import logging
import uuid
from datetime import UTC, datetime
from typing import Literal

from app.core.config import get_settings
from app.core.deps import RequestContext, get_db, require_permission
from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.core.permissions import Permissions
from app.integrations import twilio_workspace as tw
from app.integrations.credentials import TwilioCredentials, get_twilio_credentials
from app.integrations.twilio_client import build_stream_twiml
from app.schemas.workspace import (
    AgentProfileRead,
    AgentProfileUpdate,
    AgentWithUser,
    BusinessHoursPayload,
    CallbackRequest,
    ConferenceAddRequest,
    ControlResult,
    HandoffPackage,
    HolidayCreate,
    HolidayRead,
    LiveBoard,
    RoutingPreview,
    RoutingTargetRead,
    StatusUpdate,
    SupervisorJoinRequest,
    TransferRequest,
)
from app.services.call_analysis import analyze_call
from app.services.call_finalize import finalize_call
from app.services.routing import active_call_counts, pick_targets
from app.services.workspace import (
    LIVE_STATUSES,
    build_handoff_package,
    build_live_board,
    get_handoff,
)
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Call,
    CallDirection,
    CallEvent,
    CallStatus,
    Customer,
    MembershipStatus,
    Ticket,
    TicketPriority,
    User,
)
from packages.database.models import TenantUser
from packages.database.models.workspace import (
    AgentProfile,
    BusinessHours,
    CallHandoff,
    HandlerState,
    Holiday,
    HumanAgentStatus,
)

router = APIRouter(prefix="/workspace", tags=["workspace"])
logger = logging.getLogger(__name__)

AGENT_LEG_TERMINAL_UNANSWERED = {"no-answer", "busy", "failed", "canceled"}


class BadRequestError(AppError):
    status_code = 400
    detail = "Bad request"


class SummaryUnavailableError(AppError):
    status_code = 503
    detail = "The AI summary could not be generated right now; try again shortly"


class TwilioUnavailableError(AppError):
    status_code = 400
    detail = "Twilio is not configured"


class TwilioUpstreamError(AppError):
    status_code = 502
    detail = "Twilio rejected the request"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


async def _creds(db: AsyncSession, tenant_id: uuid.UUID) -> TwilioCredentials:
    credentials = await get_twilio_credentials(db, tenant_id)
    try:
        tw.require_credentials(credentials)
    except tw.TwilioClientError as exc:
        raise TwilioUnavailableError(str(exc)) from exc
    return credentials


async def _twilio(fn, **kwargs):
    try:
        return await tw.run_twilio(fn, **kwargs)
    except tw.TwilioClientError as exc:
        raise TwilioUpstreamError(str(exc)) from exc
    except tw.TwilioException as exc:
        raise TwilioUpstreamError(f"Twilio error: {getattr(exc, 'msg', str(exc))}") from exc


async def _load_call(db: AsyncSession, ctx: RequestContext, call_id: uuid.UUID) -> Call:
    call = (
        await db.execute(select(Call).where(Call.id == call_id, Call.tenant_id == ctx.tenant_id))
    ).scalar_one_or_none()
    if call is None:
        raise NotFoundError("Call not found")
    return call


def _require_live(call: Call) -> None:
    if call.status not in LIVE_STATUSES:
        raise ConflictError("This call is no longer live")


async def _get_or_create_profile(
    db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> AgentProfile:
    profile = (
        await db.execute(
            select(AgentProfile).where(
                AgentProfile.tenant_id == tenant_id, AgentProfile.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if profile is None:
        profile = AgentProfile(
            tenant_id=tenant_id,
            user_id=user_id,
            status=HumanAgentStatus.OFFLINE,
            skills=[],
            languages=[],
            departments=[],
            is_vip_handler=False,
            max_concurrent=1,
        )
        db.add(profile)
        await db.flush()
    return profile


def _our_number(call: Call) -> str:
    number = call.to_number if call.direction == CallDirection.INBOUND else call.from_number
    if not number:
        raise BadRequestError("The call has no business phone number to dial from")
    return number


def _callback_url() -> str:
    return f"{get_settings().public_base_url}/api/v1/workspace/webhooks/agent-leg"


def _conference_name(call: Call) -> str:
    return f"call-{call.id.hex}"


def _result(handoff: CallHandoff | None, call: Call, detail: str | None = None) -> ControlResult:
    return ControlResult(
        call_id=call.id,
        state=handoff.state if handoff else HandlerState.AI,
        on_hold=bool(handoff and handoff.on_hold),
        muted=bool(handoff and handoff.muted),
        detail=detail,
    )


async def _my_live_handoff(
    db: AsyncSession, ctx: RequestContext, call: Call, *, need_agent_leg: bool = False
) -> CallHandoff:
    """The handoff row of a live call this user currently controls."""
    _require_live(call)
    handoff = await get_handoff(db, call.id)
    if handoff is None or handoff.state != HandlerState.HUMAN or not handoff.conference_name:
        raise ConflictError("Take over the call first — it is still handled by the AI agent")
    if handoff.handler_user_id not in (None, ctx.user_id):
        raise ConflictError("Another agent is handling this call")
    if need_agent_leg and not handoff.agent_call_sid:
        raise ConflictError("No agent leg is connected to this call")
    return handoff


async def _target_phone(db: AsyncSession, tenant_id: uuid.UUID, user_id, phone) -> str:
    if phone:
        return phone
    profile = (
        await db.execute(
            select(AgentProfile).where(
                AgentProfile.tenant_id == tenant_id, AgentProfile.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if profile is None or not profile.phone_number:
        raise BadRequestError("That agent has no phone number configured")
    return profile.phone_number


def _log(db: AsyncSession, call: Call, type_: str, payload: dict) -> None:
    db.add(CallEvent(tenant_id=call.tenant_id, call_id=call.id, type=type_, payload=payload))


# --------------------------------------------------------------------------
# agent profiles and presence
# --------------------------------------------------------------------------


@router.get("/me", response_model=AgentProfileRead)
async def get_my_profile(
    ctx: RequestContext = Depends(require_permission(Permissions.CALLS_READ)),
    db: AsyncSession = Depends(get_db),
) -> AgentProfile:
    profile = await _get_or_create_profile(db, ctx.tenant_id, ctx.user_id)
    await db.commit()
    await db.refresh(profile)
    return profile


@router.put("/me/status", response_model=AgentProfileRead)
async def set_my_status(
    payload: StatusUpdate,
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> AgentProfile:
    profile = await _get_or_create_profile(db, ctx.tenant_id, ctx.user_id)
    if payload.phone_number:
        profile.phone_number = payload.phone_number
    if payload.status == HumanAgentStatus.AVAILABLE and not profile.phone_number:
        raise BadRequestError("Add a phone number before going available — calls ring that number")
    if profile.status != payload.status:
        profile.status = payload.status
        profile.status_changed_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(profile)
    return profile


@router.get("/agents", response_model=list[AgentWithUser])
async def list_agents(
    ctx: RequestContext = Depends(require_permission(Permissions.CALLS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[AgentWithUser]:
    members = (
        await db.execute(
            select(User)
            .join(TenantUser, TenantUser.user_id == User.id)
            .where(
                TenantUser.tenant_id == ctx.tenant_id,
                TenantUser.status == MembershipStatus.ACTIVE,
            )
            .order_by(User.full_name)
        )
    ).scalars().all()
    profiles = {
        p.user_id: p
        for p in (
            await db.execute(select(AgentProfile).where(AgentProfile.tenant_id == ctx.tenant_id))
        ).scalars()
    }
    counts = await active_call_counts(db, ctx.tenant_id)
    out: list[AgentWithUser] = []
    for user in members:
        p = profiles.get(user.id)
        out.append(
            AgentWithUser(
                user_id=user.id,
                full_name=user.full_name,
                email=user.email,
                status=p.status if p else HumanAgentStatus.OFFLINE,
                phone_number=p.phone_number if p else None,
                skills=list(p.skills) if p else [],
                languages=list(p.languages) if p else [],
                departments=list(p.departments) if p else [],
                is_vip_handler=p.is_vip_handler if p else False,
                max_concurrent=p.max_concurrent if p else 1,
                status_changed_at=p.status_changed_at if p else None,
                active_calls=counts.get(user.id, 0),
            )
        )
    return out


@router.put("/agents/{user_id}", response_model=AgentProfileRead)
async def update_agent_profile(
    user_id: uuid.UUID,
    payload: AgentProfileUpdate,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> AgentProfile:
    member = (
        await db.execute(
            select(TenantUser).where(
                TenantUser.tenant_id == ctx.tenant_id, TenantUser.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise NotFoundError("Team member not found")
    profile = await _get_or_create_profile(db, ctx.tenant_id, user_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(profile, field, value)
    await db.commit()
    await db.refresh(profile)
    return profile


# --------------------------------------------------------------------------
# routing configuration
# --------------------------------------------------------------------------


@router.get("/business-hours", response_model=BusinessHoursPayload)
async def get_business_hours(
    ctx: RequestContext = Depends(require_permission(Permissions.CALLS_READ)),
    db: AsyncSession = Depends(get_db),
) -> BusinessHoursPayload:
    row = (
        await db.execute(select(BusinessHours).where(BusinessHours.tenant_id == ctx.tenant_id))
    ).scalar_one_or_none()
    if row is None:
        return BusinessHoursPayload()
    return BusinessHoursPayload(
        timezone=row.timezone,
        windows=row.windows,
        fallback_action=row.fallback_action,
        fallback_message=row.fallback_message,
    )


@router.put("/business-hours", response_model=BusinessHoursPayload)
async def put_business_hours(
    payload: BusinessHoursPayload,
    ctx: RequestContext = Depends(require_permission(Permissions.SETTINGS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> BusinessHoursPayload:
    row = (
        await db.execute(select(BusinessHours).where(BusinessHours.tenant_id == ctx.tenant_id))
    ).scalar_one_or_none()
    if row is None:
        row = BusinessHours(tenant_id=ctx.tenant_id)
        db.add(row)
    row.timezone = payload.timezone
    row.windows = [w.model_dump() for w in payload.windows]
    row.fallback_action = payload.fallback_action
    row.fallback_message = payload.fallback_message
    await db.commit()
    return payload


@router.get("/holidays", response_model=list[HolidayRead])
async def list_holidays(
    ctx: RequestContext = Depends(require_permission(Permissions.CALLS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[Holiday]:
    return list(
        (
            await db.execute(
                select(Holiday).where(Holiday.tenant_id == ctx.tenant_id).order_by(Holiday.day)
            )
        ).scalars()
    )


@router.post("/holidays", response_model=HolidayRead, status_code=status.HTTP_201_CREATED)
async def create_holiday(
    payload: HolidayCreate,
    ctx: RequestContext = Depends(require_permission(Permissions.SETTINGS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Holiday:
    existing = (
        await db.execute(
            select(Holiday).where(Holiday.tenant_id == ctx.tenant_id, Holiday.day == payload.day)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("That date is already a holiday")
    holiday = Holiday(tenant_id=ctx.tenant_id, day=payload.day, name=payload.name)
    db.add(holiday)
    await db.commit()
    await db.refresh(holiday)
    return holiday


@router.delete("/holidays/{holiday_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_holiday(
    holiday_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.SETTINGS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    holiday = (
        await db.execute(
            select(Holiday).where(Holiday.id == holiday_id, Holiday.tenant_id == ctx.tenant_id)
        )
    ).scalar_one_or_none()
    if holiday is None:
        raise NotFoundError("Holiday not found")
    await db.delete(holiday)
    await db.commit()


@router.get("/routing/preview", response_model=RoutingPreview)
async def preview_routing(
    language: str | None = Query(default=None, max_length=20),
    department: str | None = Query(default=None, max_length=60),
    vip: bool = False,
    issue_type: str | None = Query(default=None, max_length=60),
    ctx: RequestContext = Depends(require_permission(Permissions.CALLS_READ)),
    db: AsyncSession = Depends(get_db),
) -> RoutingPreview:
    decision = await pick_targets(
        db, ctx.tenant_id, language=language, department=department, vip=vip, issue_type=issue_type
    )
    names = {
        u.id: u.full_name
        for u in (
            await db.execute(select(User).where(User.id.in_([t.user_id for t in decision.targets])))
        ).scalars()
    }
    return RoutingPreview(
        action=decision.action,
        reason=decision.reason,
        message=decision.message,
        targets=[
            RoutingTargetRead(
                user_id=t.user_id,
                phone_number=t.phone_number,
                score=t.score,
                full_name=names.get(t.user_id),
            )
            for t in decision.targets
        ],
    )


# --------------------------------------------------------------------------
# live board and handoff package
# --------------------------------------------------------------------------


@router.get("/live", response_model=LiveBoard)
async def live_board(
    ctx: RequestContext = Depends(require_permission(Permissions.CALLS_READ)),
    db: AsyncSession = Depends(get_db),
) -> LiveBoard:
    return await build_live_board(db, ctx.tenant_id)


@router.get("/calls/{call_id}/handoff", response_model=HandoffPackage)
async def get_handoff_package(
    call_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CALLS_READ)),
    db: AsyncSession = Depends(get_db),
) -> HandoffPackage:
    call = await _load_call(db, ctx, call_id)
    return await build_handoff_package(db, ctx.tenant_id, call)


@router.post("/calls/{call_id}/handoff/summarize", response_model=HandoffPackage)
async def summarize_for_handoff(
    call_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> HandoffPackage:
    """Generates (or refreshes) the AI summary/sentiment/intent for the call so
    far, reusing the post-call analysis pass."""
    call = await _load_call(db, ctx, call_id)
    if not await analyze_call(db, call.id):
        raise SummaryUnavailableError()
    await db.refresh(call)
    return await build_handoff_package(db, ctx.tenant_id, call)


# --------------------------------------------------------------------------
# call control
# --------------------------------------------------------------------------


@router.post("/calls/{call_id}/takeover", response_model=ControlResult)
async def take_over_call(
    call_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> ControlResult:
    """Moves the caller into a conference, rings the agent's own phone into it
    and stops the AI from replying (state=human)."""
    call = await _load_call(db, ctx, call_id)
    _require_live(call)
    if not call.provider_call_sid:
        raise ConflictError("This call has no Twilio leg to take over")
    credentials = await _creds(db, ctx.tenant_id)
    profile = await _get_or_create_profile(db, ctx.tenant_id, ctx.user_id)
    if not profile.phone_number:
        raise BadRequestError("Add your phone number in your agent profile — the call rings there")
    from_number = _our_number(call)

    handoff = await get_handoff(db, call.id)
    if handoff is None:
        handoff = CallHandoff(tenant_id=ctx.tenant_id, call_id=call.id)
        db.add(handoff)
    elif handoff.state == HandlerState.HUMAN and handoff.handler_user_id not in (None, ctx.user_id):
        raise ConflictError("Another agent already has this call")

    name = _conference_name(call)
    handoff.state = HandlerState.HUMAN
    handoff.handler_user_id = ctx.user_id
    handoff.conference_name = name
    handoff.on_hold = False
    handoff.muted = False
    handoff.taken_at = datetime.now(UTC)
    handoff.returned_at = None
    handoff.handoff_reason = "agent_takeover"
    # Committed before any Twilio call so the AI stops replying immediately.
    await db.commit()

    agent_sid = None
    try:
        agent_sid = await _twilio(
            tw.dial_leg,
            to=profile.phone_number,
            from_=from_number,
            twiml=tw.build_conference_twiml(name, start_on_enter=True, end_on_exit=False),
            status_callback_url=_callback_url(),
            credentials=credentials,
        )
        await _twilio(
            tw.redirect_to_twiml,
            call_sid=call.provider_call_sid,
            twiml=tw.build_conference_twiml(name, start_on_enter=False, end_on_exit=True),
            credentials=credentials,
        )
    except AppError:
        handoff.state = HandlerState.AI
        handoff.handler_user_id = None
        handoff.conference_name = None
        await db.commit()
        if agent_sid:
            try:
                await tw.run_twilio(tw.hangup_leg, call_sid=agent_sid, credentials=credentials)
            except Exception:
                logger.warning("could not hang up agent leg %s", agent_sid, exc_info=True)
        raise

    handoff.agent_call_sid = agent_sid
    _log(db, call, "HUMAN_TAKEOVER", {"user_id": str(ctx.user_id)})
    await db.commit()
    return _result(handoff, call, "Calling your phone — answer to join the caller")


@router.post("/calls/{call_id}/return-to-ai", response_model=ControlResult)
async def return_to_ai(
    call_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> ControlResult:
    call = await _load_call(db, ctx, call_id)
    handoff = await _my_live_handoff(db, ctx, call)
    credentials = await _creds(db, ctx.tenant_id)
    agent_sid = handoff.agent_call_sid
    await _twilio(
        tw.redirect_to_twiml,
        call_sid=call.provider_call_sid,
        twiml=build_stream_twiml(call_id=str(call.id), tenant_id=str(call.tenant_id)),
        credentials=credentials,
    )
    handoff.state = HandlerState.AI
    handoff.handler_user_id = None
    handoff.conference_name = None
    handoff.agent_call_sid = None
    handoff.on_hold = False
    handoff.muted = False
    handoff.returned_at = datetime.now(UTC)
    _log(db, call, "RETURNED_TO_AI", {"user_id": str(ctx.user_id)})
    await db.commit()
    if agent_sid:
        try:
            await tw.run_twilio(tw.hangup_leg, call_sid=agent_sid, credentials=credentials)
        except Exception:
            logger.warning("could not hang up agent leg %s", agent_sid, exc_info=True)
    return _result(handoff, call, "The AI agent is handling the call again")


async def _set_hold(call_id: uuid.UUID, hold: bool, ctx: RequestContext, db: AsyncSession):
    call = await _load_call(db, ctx, call_id)
    handoff = await _my_live_handoff(db, ctx, call)
    credentials = await _creds(db, ctx.tenant_id)
    await _twilio(
        tw.update_participant,
        conference_name=handoff.conference_name,
        call_sid=call.provider_call_sid,
        hold=hold,
        credentials=credentials,
    )
    handoff.on_hold = hold
    await db.commit()
    return _result(handoff, call)


async def _set_mute(call_id: uuid.UUID, muted: bool, ctx: RequestContext, db: AsyncSession):
    call = await _load_call(db, ctx, call_id)
    handoff = await _my_live_handoff(db, ctx, call, need_agent_leg=True)
    credentials = await _creds(db, ctx.tenant_id)
    await _twilio(
        tw.update_participant,
        conference_name=handoff.conference_name,
        call_sid=handoff.agent_call_sid,
        muted=muted,
        credentials=credentials,
    )
    handoff.muted = muted
    await db.commit()
    return _result(handoff, call)


@router.post("/calls/{call_id}/hold", response_model=ControlResult)
async def hold_call(
    call_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> ControlResult:
    return await _set_hold(call_id, True, ctx, db)


@router.post("/calls/{call_id}/unhold", response_model=ControlResult)
async def unhold_call(
    call_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> ControlResult:
    return await _set_hold(call_id, False, ctx, db)


@router.post("/calls/{call_id}/mute", response_model=ControlResult)
async def mute_call(
    call_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> ControlResult:
    return await _set_mute(call_id, True, ctx, db)


@router.post("/calls/{call_id}/unmute", response_model=ControlResult)
async def unmute_call(
    call_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> ControlResult:
    return await _set_mute(call_id, False, ctx, db)


async def _add_to_conference(
    db: AsyncSession, ctx: RequestContext, call: Call, handoff: CallHandoff, phone: str
) -> str:
    credentials = await _creds(db, ctx.tenant_id)
    return await _twilio(
        tw.dial_leg,
        to=phone,
        from_=_our_number(call),
        twiml=tw.build_conference_twiml(
            handoff.conference_name, start_on_enter=True, end_on_exit=False
        ),
        status_callback_url=_callback_url(),
        credentials=credentials,
    )


@router.post("/calls/{call_id}/conference/add", response_model=ControlResult)
async def add_to_conference(
    call_id: uuid.UUID,
    payload: ConferenceAddRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> ControlResult:
    call = await _load_call(db, ctx, call_id)
    handoff = await _my_live_handoff(db, ctx, call)
    phone = await _target_phone(db, ctx.tenant_id, payload.user_id, payload.phone_number)
    sid = await _add_to_conference(db, ctx, call, handoff, phone)
    _log(db, call, "CONFERENCE_ADD", {"to": phone, "leg_sid": sid, "by": str(ctx.user_id)})
    await db.commit()
    return _result(handoff, call, f"Calling {phone} into the call")


@router.post("/calls/{call_id}/transfer", response_model=ControlResult)
async def transfer_call_to_human(
    call_id: uuid.UUID,
    payload: TransferRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> ControlResult:
    """warm: the third party joins and the agent stays until they drop. cold:
    the third party joins and the agent's leg is dropped immediately."""
    call = await _load_call(db, ctx, call_id)
    handoff = await _my_live_handoff(db, ctx, call)
    phone = await _target_phone(db, ctx.tenant_id, payload.user_id, payload.phone_number)
    sid = await _add_to_conference(db, ctx, call, handoff, phone)
    _log(
        db,
        call,
        "TRANSFER_" + payload.mode.upper(),
        {"to": phone, "leg_sid": sid, "by": str(ctx.user_id)},
    )
    detail = f"Calling {phone} into the call"
    if payload.mode == "cold":
        old_sid = handoff.agent_call_sid
        # Re-point the handoff at the new leg first so the old leg's
        # completion webhook doesn't look like the call ending.
        handoff.agent_call_sid = sid
        handoff.handler_user_id = payload.user_id
        await db.commit()
        if old_sid:
            credentials = await _creds(db, ctx.tenant_id)
            await _twilio(tw.hangup_leg, call_sid=old_sid, credentials=credentials)
        detail = f"Transferred to {phone}"
    else:
        await db.commit()
    return _result(handoff, call, detail)


@router.post("/calls/{call_id}/callback", response_model=ControlResult)
async def request_callback(
    call_id: uuid.UUID,
    payload: CallbackRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.CONVERSATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> ControlResult:
    """Logs a callback commitment: an event on the call plus, when the caller
    is a known customer, a ticket so it shows up in the team's queue."""
    call = await _load_call(db, ctx, call_id)
    ticket_id = None
    if call.customer_id:
        customer = await db.get(Customer, call.customer_id)
        ticket = Ticket(
            tenant_id=ctx.tenant_id,
            customer_id=call.customer_id,
            assigned_to_user_id=ctx.user_id,
            subject=f"Callback requested: {customer.name if customer else call.from_number}"[:255],
            priority=TicketPriority(payload.priority),
        )
        db.add(ticket)
        await db.flush()
        from app.services.operations import apply_sla_safely

        await apply_sla_safely(db, ctx.tenant_id, ticket)
        ticket_id = str(ticket.id)
    _log(
        db,
        call,
        "CALLBACK_REQUESTED",
        {"note": payload.note, "ticket_id": ticket_id, "by": str(ctx.user_id)},
    )
    await db.commit()
    handoff = await get_handoff(db, call.id)
    return _result(handoff, call, "Callback logged" + (" and ticket created" if ticket_id else ""))


@router.post("/calls/{call_id}/supervisor/{mode}", response_model=ControlResult)
async def supervisor_join(
    call_id: uuid.UUID,
    mode: Literal["listen", "whisper", "barge"],
    payload: SupervisorJoinRequest | None = None,
    ctx: RequestContext = Depends(require_permission(Permissions.CALLS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> ControlResult:
    """Rings the supervisor's phone into the call's conference: listen (muted),
    whisper (only the agent hears them), barge (everyone hears them). Needs a
    human-controlled call — AI-handled audio is a media stream, not a
    conference; use take over for those."""
    call = await _load_call(db, ctx, call_id)
    _require_live(call)
    handoff = await get_handoff(db, call.id)
    if handoff is None or handoff.state != HandlerState.HUMAN or not handoff.conference_name:
        raise ConflictError(
            "Listen/whisper/barge need a human-handled call. Take over the AI call first."
        )
    if mode == "whisper" and not handoff.agent_call_sid:
        raise ConflictError("No agent is on this call to whisper to")
    phone = payload.phone_number if payload and payload.phone_number else None
    if phone is None:
        profile = await _get_or_create_profile(db, ctx.tenant_id, ctx.user_id)
        phone = profile.phone_number
    if not phone:
        raise BadRequestError("Add your phone number in your agent profile — the call rings there")
    credentials = await _creds(db, ctx.tenant_id)
    twiml = tw.build_conference_twiml(
        handoff.conference_name,
        start_on_enter=False,
        end_on_exit=False,
        muted=mode == "listen",
        coach=handoff.agent_call_sid if mode == "whisper" else None,
    )
    sid = await _twilio(
        tw.dial_leg, to=phone, from_=_our_number(call), twiml=twiml, credentials=credentials
    )
    _log(db, call, f"SUPERVISOR_{mode.upper()}", {"leg_sid": sid, "by": str(ctx.user_id)})
    await db.commit()
    return _result(handoff, call, f"Calling your phone to {mode}")


# --------------------------------------------------------------------------
# Twilio callback for agent legs
# --------------------------------------------------------------------------


@router.post("/webhooks/agent-leg")
async def agent_leg_status(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    """Twilio posts here when the agent's leg ends. Unanswered legs hand the
    caller back to the AI; otherwise, if the caller is gone too, the call is
    finalized (the gateway's own end signal is ignored while a human holds it)."""
    from app.api.v1.telephony import _resolve_auth_token, _validate_request

    form = dict((await request.form()).items())
    leg_sid = form.get("CallSid")
    handoff = (
        await db.execute(select(CallHandoff).where(CallHandoff.agent_call_sid == leg_sid))
    ).scalar_one_or_none()
    auth_token = await _resolve_auth_token(db, handoff.tenant_id if handoff else None)
    await _validate_request(request, form, auth_token=auth_token)
    if handoff is None or handoff.state != HandlerState.HUMAN:
        return PlainTextResponse("")

    call = await db.get(Call, handoff.call_id)
    if call is None or call.status not in LIVE_STATUSES:
        return PlainTextResponse("")
    call_status = form.get("CallStatus", "")
    credentials = await get_twilio_credentials(db, call.tenant_id)

    if call_status in AGENT_LEG_TERMINAL_UNANSWERED and call.provider_call_sid:
        handoff.state = HandlerState.AI
        handoff.handler_user_id = None
        handoff.conference_name = None
        handoff.agent_call_sid = None
        handoff.returned_at = datetime.now(UTC)
        await db.commit()
        try:
            await tw.run_twilio(
                tw.redirect_to_twiml,
                call_sid=call.provider_call_sid,
                twiml=build_stream_twiml(call_id=str(call.id), tenant_id=str(call.tenant_id)),
                credentials=credentials,
            )
        except Exception:
            logger.warning("could not return call %s to AI", call.id, exc_info=True)
        return PlainTextResponse("")

    customer_gone = True
    if call.provider_call_sid:
        try:
            customer_gone = (
                await tw.run_twilio(
                    tw.call_status, call_sid=call.provider_call_sid, credentials=credentials
                )
            ) in {"completed", "failed", "busy", "no-answer", "canceled"}
        except Exception:
            logger.warning("could not read caller leg status for %s", call.id, exc_info=True)
    if customer_gone:
        handoff.returned_at = datetime.now(UTC)
        await finalize_call(db, call, status=CallStatus.COMPLETED, reason="human_handled")
    return PlainTextResponse("")
