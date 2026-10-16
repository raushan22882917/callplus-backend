"""telephony_plus: IVR/auto-attendant, queues, ring groups, forwarding, DND,
dialer settings + campaign analytics/export, SIP trunks, number routing,
call parking and number-masked bridging.

Two kinds of endpoints live here: tenant-authenticated management APIs
(require_permission) and Twilio webhooks (IVR digits, queue wait/result, ring
group fallthrough, agent legs). Webhooks are unauthenticated by JWT and verified
with Twilio's request signature exactly like app/api/v1/telephony.py — the
owning tenant is always derived from the stored row, never from the request.
"""

import csv
import io
import uuid
from datetime import UTC, datetime

from app.api.v1.telephony import _resolve_auth_token, _validate_request
from app.core.audit import log_audit_event
from app.core.config import get_settings
from app.core.deps import RequestContext, get_db, require_permission
from app.core.exceptions import NotFoundError
from app.core.permissions import Permissions
from app.core.repository import TenantScopedRepository
from app.integrations import twilio_plus
from app.integrations.credentials import get_twilio_credentials
from app.integrations.twilio_client import CallNotLiveError, TwilioClientError, place_outbound_call
from app.schemas import telephony_plus as schemas
from app.services import dialer, ivr
from app.services import telephony_plus as svc
from app.services.customer_lookup import find_or_create_customer_by_phone
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.base.exceptions import TwilioException

from packages.database.models import (
    AIAgent,
    Call,
    CallDirection,
    CallStatus,
    Campaign,
    CampaignContact,
    Customer,
    PhoneNumber,
)
from packages.database.models.telephony_plus import (
    CallForwardingRule,
    CallQueue,
    CampaignDialerSettings,
    CampaignOutcome,
    DndEntry,
    IvrMenu,
    NumberRouting,
    ParkedCall,
    RingGroup,
    SipTrunk,
)

router = APIRouter(prefix="/telephony_plus", tags=["telephony-plus"])

READ = Permissions.CALLS_READ
WRITE = Permissions.SETTINGS_MANAGE


def _xml(twiml: str) -> Response:
    return Response(content=twiml, media_type="application/xml")


async def _commit(db: AsyncSession) -> None:
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "A record with these values already exists") from exc


def _register_crud(
    *,
    path: str,
    model,
    create_schema,
    update_schema,
    read_schema,
    read_perm,
    write_perm,
    audit: str,
    validate=None,
) -> None:
    """Standard tenant-scoped list/create/get/patch/delete for a model."""

    class Repo(TenantScopedRepository[model]):
        pass

    Repo.model = model

    @router.get(path, response_model=list[read_schema], name=f"list_{audit}")
    async def list_items(
        limit: int = Query(default=100, le=500),
        offset: int = Query(default=0, ge=0),
        ctx: RequestContext = Depends(require_permission(read_perm)),
        db: AsyncSession = Depends(get_db),
    ):
        return await Repo(db, ctx.tenant_id).list(limit=limit, offset=offset)

    @router.post(path, response_model=read_schema, status_code=201, name=f"create_{audit}")
    async def create_item(
        payload: create_schema,
        ctx: RequestContext = Depends(require_permission(write_perm)),
        db: AsyncSession = Depends(get_db),
    ):
        data = payload.model_dump()
        if validate:
            await validate(db, ctx.tenant_id, data)
        try:
            obj = await Repo(db, ctx.tenant_id).create(**data)
        except IntegrityError as exc:
            await db.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT, "A record with these values already exists"
            ) from exc
        await log_audit_event(
            db,
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user_id,
            action=f"{audit}.created",
            resource_type=audit,
            resource_id=obj.id,
        )
        await _commit(db)
        await db.refresh(obj)
        return obj

    @router.get(path + "/{item_id}", response_model=read_schema, name=f"get_{audit}")
    async def get_item(
        item_id: uuid.UUID,
        ctx: RequestContext = Depends(require_permission(read_perm)),
        db: AsyncSession = Depends(get_db),
    ):
        return await Repo(db, ctx.tenant_id).get(item_id)

    @router.patch(path + "/{item_id}", response_model=read_schema, name=f"update_{audit}")
    async def update_item(
        item_id: uuid.UUID,
        payload: update_schema,
        ctx: RequestContext = Depends(require_permission(write_perm)),
        db: AsyncSession = Depends(get_db),
    ):
        obj = await Repo(db, ctx.tenant_id).get(item_id)
        data = payload.model_dump(exclude_unset=True)
        if validate:
            await validate(db, ctx.tenant_id, data)
        for key, value in data.items():
            setattr(obj, key, value)
        await log_audit_event(
            db,
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user_id,
            action=f"{audit}.updated",
            resource_type=audit,
            resource_id=obj.id,
        )
        await _commit(db)
        await db.refresh(obj)
        return obj

    @router.delete(path + "/{item_id}", status_code=204, name=f"delete_{audit}")
    async def delete_item(
        item_id: uuid.UUID,
        ctx: RequestContext = Depends(require_permission(write_perm)),
        db: AsyncSession = Depends(get_db),
    ) -> Response:
        await Repo(db, ctx.tenant_id).delete(item_id)
        await log_audit_event(
            db,
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user_id,
            action=f"{audit}.deleted",
            resource_type=audit,
            resource_id=item_id,
        )
        await _commit(db)
        return Response(status_code=204)


async def _validate_ivr(db: AsyncSession, tenant_id: uuid.UUID, data: dict) -> None:
    """Every option/after-hours action must point at something this tenant owns."""
    actions = list((data.get("options") or {}).values())
    if data.get("after_hours_action"):
        actions.append(data["after_hours_action"])
    models = {"queue": CallQueue, "ring_group": RingGroup, "ai_agent": AIAgent, "submenu": IvrMenu}
    for action in actions:
        model = models.get(action.get("action"))
        if model is None or not action.get("target_id"):
            continue
        try:
            target = await db.get(model, uuid.UUID(str(action["target_id"])))
        except ValueError:
            target = None
        if target is None or target.tenant_id != tenant_id:
            raise HTTPException(
                422,
                f"{action['action']} target {action['target_id']} not found",
            )


async def _validate_queue(db: AsyncSession, tenant_id: uuid.UUID, data: dict) -> None:
    overflow = data.get("overflow_action")
    if overflow and overflow.get("action") == "forward" and not overflow.get("number"):
        raise HTTPException(422, "forward overflow requires number")
    if overflow and overflow.get("action") in ("ring_group", "ai_agent") and not overflow.get("target_id"):
        raise HTTPException(422, f"{overflow['action']} overflow requires target_id")


_register_crud(
    path="/ivr-menus",
    model=IvrMenu,
    create_schema=schemas.IvrMenuCreate,
    update_schema=schemas.IvrMenuUpdate,
    read_schema=schemas.IvrMenuRead,
    read_perm=READ,
    write_perm=WRITE,
    audit="ivr_menu",
    validate=_validate_ivr,
)
_register_crud(
    path="/queues",
    model=CallQueue,
    create_schema=schemas.QueueCreate,
    update_schema=schemas.QueueUpdate,
    read_schema=schemas.QueueRead,
    read_perm=READ,
    write_perm=WRITE,
    audit="call_queue",
    validate=_validate_queue,
)
_register_crud(
    path="/ring-groups",
    model=RingGroup,
    create_schema=schemas.RingGroupCreate,
    update_schema=schemas.RingGroupUpdate,
    read_schema=schemas.RingGroupRead,
    read_perm=READ,
    write_perm=WRITE,
    audit="ring_group",
)
_register_crud(
    path="/forwarding-rules",
    model=CallForwardingRule,
    create_schema=schemas.ForwardingRuleCreate,
    update_schema=schemas.ForwardingRuleUpdate,
    read_schema=schemas.ForwardingRuleRead,
    read_perm=READ,
    write_perm=WRITE,
    audit="forwarding_rule",
)
_register_crud(
    path="/number-routing",
    model=NumberRouting,
    create_schema=schemas.NumberRoutingCreate,
    update_schema=schemas.NumberRoutingUpdate,
    read_schema=schemas.NumberRoutingRead,
    read_perm=READ,
    write_perm=WRITE,
    audit="number_routing",
)


# --- DND registry -----------------------------------------------------------------------


@router.get("/dnd", response_model=list[schemas.DndRead])
async def list_dnd(
    limit: int = Query(default=200, le=1000),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(require_permission(READ)),
    db: AsyncSession = Depends(get_db),
) -> list[DndEntry]:
    result = await db.execute(
        select(DndEntry)
        .where(DndEntry.tenant_id == ctx.tenant_id)
        .order_by(DndEntry.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


@router.post("/dnd", response_model=schemas.DndRead, status_code=201)
async def add_dnd(
    payload: schemas.DndCreate,
    ctx: RequestContext = Depends(require_permission(WRITE)),
    db: AsyncSession = Depends(get_db),
) -> DndEntry:
    entry = DndEntry(tenant_id=ctx.tenant_id, e164_number=payload.e164_number, note=payload.note)
    db.add(entry)
    await _commit(db)
    await db.refresh(entry)
    return entry


@router.delete("/dnd/{entry_id}", status_code=204)
async def remove_dnd(
    entry_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(WRITE)),
    db: AsyncSession = Depends(get_db),
) -> Response:
    entry = await db.get(DndEntry, entry_id)
    if entry is None or entry.tenant_id != ctx.tenant_id:
        raise NotFoundError("DND entry not found")
    await db.delete(entry)
    await _commit(db)
    return Response(status_code=204)


@router.post("/dnd/check", response_model=schemas.DndCheckResponse)
async def check_dnd(
    payload: schemas.DndCheckRequest,
    ctx: RequestContext = Depends(require_permission(READ)),
    db: AsyncSession = Depends(get_db),
) -> schemas.DndCheckResponse:
    """Would an outbound call to this number be allowed right now? Applies the
    blocklist, DND registry and (default) TRAI 9pm-9am quiet hours in the
    number's own timezone."""
    kwargs: dict = {}
    if payload.enforce_quiet_hours:
        kwargs = {
            "quiet_hours": ("21:00", "09:00"),
            "tz_name": payload.timezone or dialer.timezone_for_contact(payload.e164_number),
        }
    decision = await svc.check_outbound_allowed(db, ctx.tenant_id, payload.e164_number, **kwargs)
    return schemas.DndCheckResponse(allowed=decision.allowed, reason=decision.reason)


# --- SIP trunks ---------------------------------------------------------------------------


class _SipRepo(TenantScopedRepository[SipTrunk]):
    model = SipTrunk


@router.get("/sip-trunks", response_model=list[schemas.SipTrunkRead])
async def list_sip_trunks(
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[SipTrunk]:
    return await _SipRepo(db, ctx.tenant_id).list(limit=200)


@router.post("/sip-trunks", response_model=schemas.SipTrunkRead, status_code=201)
async def create_sip_trunk(
    payload: schemas.SipTrunkCreate,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> SipTrunk:
    trunk = await _SipRepo(db, ctx.tenant_id).create(**payload.model_dump())
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="sip_trunk.created",
        resource_type="sip_trunk",
        resource_id=trunk.id,
    )
    await _commit(db)
    await db.refresh(trunk)
    return trunk


@router.get("/sip-trunks/{trunk_id}", response_model=schemas.SipTrunkRead)
async def get_sip_trunk(
    trunk_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_READ)),
    db: AsyncSession = Depends(get_db),
) -> SipTrunk:
    return await _SipRepo(db, ctx.tenant_id).get(trunk_id)


@router.patch("/sip-trunks/{trunk_id}", response_model=schemas.SipTrunkRead)
async def update_sip_trunk(
    trunk_id: uuid.UUID,
    payload: schemas.SipTrunkUpdate,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> SipTrunk:
    trunk = await _SipRepo(db, ctx.tenant_id).get(trunk_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(trunk, key, value)
    trunk.sync_status = "unsynced" if trunk.twilio_trunk_sid else trunk.sync_status
    await _commit(db)
    await db.refresh(trunk)
    return trunk


def _twilio_credentials_or_400(credentials) -> None:
    if not credentials.account_sid or not credentials.auth_token:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Twilio credentials are not configured: connect Twilio under Integrations first",
        )


@router.delete("/sip-trunks/{trunk_id}", status_code=204)
async def delete_sip_trunk(
    trunk_id: uuid.UUID,
    local_only: bool = Query(default=False),
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Response:
    trunk = await _SipRepo(db, ctx.tenant_id).get(trunk_id)
    if trunk.twilio_trunk_sid and not local_only:
        credentials = await get_twilio_credentials(db, ctx.tenant_id)
        _twilio_credentials_or_400(credentials)
        try:
            twilio_plus.delete_elastic_sip_trunk(
                trunk_sid=trunk.twilio_trunk_sid, credentials=credentials
            )
        except (TwilioClientError, TwilioException) as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"Twilio refused to delete the trunk ({exc}); retry with local_only=true to forget it here",
            ) from exc
    await db.delete(trunk)
    await _commit(db)
    return Response(status_code=204)


@router.post("/sip-trunks/{trunk_id}/sync", response_model=schemas.SipTrunkRead)
async def sync_sip_trunk(
    trunk_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> SipTrunk:
    """Creates/updates the Twilio Elastic SIP trunk for this record."""
    trunk = await _SipRepo(db, ctx.tenant_id).get(trunk_id)
    if trunk.provider != "twilio":
        raise HTTPException(400, "Only Twilio trunks can be synced; this trunk's provider is " + trunk.provider)
    credentials = await get_twilio_credentials(db, ctx.tenant_id)
    _twilio_credentials_or_400(credentials)
    try:
        result = twilio_plus.sync_elastic_sip_trunk(
            label=trunk.label,
            termination_uri=trunk.termination_uri,
            origination_uri=trunk.origination_uri,
            caller_id=trunk.caller_id,
            existing_trunk_sid=trunk.twilio_trunk_sid,
            credentials=credentials,
        )
    except (TwilioClientError, TwilioException) as exc:
        trunk.sync_status = "failed"
        trunk.sync_error = str(exc)
        await db.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Twilio sync failed: {exc}") from exc

    trunk.twilio_trunk_sid = result.trunk_sid
    trunk.sync_status = "synced"
    trunk.sync_error = None
    trunk.synced_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(trunk)
    return trunk


# --- Twilio webhooks: IVR / queues / ring groups ----------------------------------------------


async def _signed_form(request: Request, db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    form = dict((await request.form()).items())
    await _validate_request(request, form, auth_token=await _resolve_auth_token(db, tenant_id))
    return form


async def _call_for_webhook(
    db: AsyncSession, request: Request, form: dict, tenant_id: uuid.UUID
) -> Call:
    call: Call | None = None
    call_id = request.query_params.get("call_id")
    if call_id:
        try:
            call = await db.get(Call, uuid.UUID(call_id))
        except ValueError:
            call = None
    elif form.get("CallSid"):
        call = (
            await db.execute(select(Call).where(Call.provider_call_sid == form["CallSid"]))
        ).scalar_one_or_none()
    if call is None or call.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Call not found")
    return call


@router.post("/ivr/{menu_id}/digit")
async def ivr_digit(
    menu_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)
) -> Response:
    menu = await db.get(IvrMenu, menu_id)
    if menu is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "IVR menu not found")
    form = await _signed_form(request, db, menu.tenant_id)
    call = await _call_for_webhook(db, request, form, menu.tenant_id)
    attempt = int(request.query_params.get("attempt", "0") or 0)
    twiml = await svc.handle_digit(db, menu, call, form.get("Digits"), attempt)
    await db.commit()
    return _xml(twiml)


async def _queue_or_404(db: AsyncSession, queue_id: uuid.UUID) -> CallQueue:
    queue = await db.get(CallQueue, queue_id)
    if queue is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Queue not found")
    return queue


@router.post("/queues/{queue_id}/wait")
async def queue_wait(
    queue_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)
) -> Response:
    """<Enqueue waitUrl>: position announcement, hold message and music; leaves
    the queue once max_wait_seconds is exceeded."""
    queue = await _queue_or_404(db, queue_id)
    form = await _signed_form(request, db, queue.tenant_id)

    def _int(key: str) -> int:
        try:
            return int(form.get(key, 0) or 0)
        except ValueError:
            return 0

    return _xml(
        ivr.build_queue_wait_twiml(
            position=_int("QueuePosition") or None,
            hold_message=queue.hold_message,
            music_url=queue.hold_music_url,
            queue_time_seconds=_int("QueueTime"),
            max_wait_seconds=queue.max_wait_seconds,
        )
    )


@router.post("/queues/{queue_id}/result")
async def queue_result(
    queue_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)
) -> Response:
    queue = await _queue_or_404(db, queue_id)
    form = await _signed_form(request, db, queue.tenant_id)
    call = await _call_for_webhook(db, request, form, queue.tenant_id)
    twiml = await svc.handle_queue_result(db, queue, call, form.get("QueueResult"))
    await db.commit()
    return _xml(twiml)


@router.post("/queues/{queue_id}/agent")
async def queue_agent_leg(
    queue_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)
) -> Response:
    """Answered agent leg: bridge to the longest-waiting caller."""
    queue = await _queue_or_404(db, queue_id)
    await _signed_form(request, db, queue.tenant_id)
    return _xml(ivr.build_queue_agent_twiml(ivr.twilio_queue_name(str(queue.tenant_id), str(queue.id))))


@router.post("/ring-groups/{group_id}/next")
async def ring_group_next(
    group_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)
) -> Response:
    """<Dial action> for a ring group: hang up if answered, try the next member
    for a sequential group, else apologise."""
    group = await db.get(RingGroup, group_id)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ring group not found")
    form = await _signed_form(request, db, group.tenant_id)
    call = await _call_for_webhook(db, request, form, group.tenant_id)

    if form.get("DialCallStatus") == "completed":
        return _xml(ivr.build_say_hangup_twiml())
    idx_raw = request.query_params.get("idx")
    if group.strategy == "sequential" and idx_raw is not None and idx_raw.isdigit():
        idx = int(idx_raw)
        if idx < len(group.members or []):
            return _xml(svc._ring_group_twiml(group, call, index=idx))
    return _xml(ivr.build_say_hangup_twiml("Sorry, nobody is available to take your call. Goodbye."))


# --- park / masked bridge ----------------------------------------------------------------------


@router.post("/park/wait")
async def park_wait(request: Request) -> Response:
    """Hold music for a parked caller. Twilio posts the ConferenceSid; the
    parked-call lookup isn't needed to play music, so the signature is verified
    against the platform token only when Twilio is configured."""
    form = dict((await request.form()).items())
    await _validate_request(request, form, auth_token=get_settings().twilio_auth_token)
    return _xml(ivr.build_hold_music_twiml())


@router.post("/park/{parked_id}/join")
async def park_join(
    parked_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)
) -> Response:
    parked = await db.get(ParkedCall, parked_id)
    if parked is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Parked call not found")
    await _signed_form(request, db, parked.tenant_id)
    return _xml(ivr.build_join_conference_twiml(_park_conference(parked)))


def _park_conference(parked: ParkedCall) -> str:
    return f"park-{str(parked.tenant_id)[:8]}-{parked.slot}"


@router.post("/calls/{call_id}/park", response_model=schemas.ParkedCallRead, status_code=201)
async def park_call(
    call_id: uuid.UUID,
    payload: schemas.ParkRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.CALLS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> ParkedCall:
    """Moves a live call into a hold conference (call parking)."""
    call = await db.get(Call, call_id)
    if call is None or call.tenant_id != ctx.tenant_id:
        raise NotFoundError("Call not found")
    if call.status != CallStatus.IN_PROGRESS or not call.provider_call_sid:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only a live, in-progress call can be parked")
    open_park = (
        await db.execute(
            select(ParkedCall).where(ParkedCall.call_id == call.id, ParkedCall.retrieved_at.is_(None))
        )
    ).scalar_one_or_none()
    if open_park is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Call is already parked")

    slot = payload.slot or uuid.uuid4().hex[:6]
    parked = ParkedCall(
        tenant_id=ctx.tenant_id, call_id=call.id, slot=slot, parked_by_user_id=ctx.user_id
    )
    credentials = await get_twilio_credentials(db, ctx.tenant_id)
    _twilio_credentials_or_400(credentials)
    try:
        twilio_plus.redirect_live_call(
            call_sid=call.provider_call_sid,
            twiml=ivr.build_park_twiml(
                conference_name=_park_conference(parked),
                wait_url=f"{svc.base_url()}/park/wait",
            ),
            credentials=credentials,
        )
    except CallNotLiveError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "The caller has already hung up, so the call can't be parked"
        ) from exc
    except (TwilioClientError, TwilioException) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Twilio could not park the call: {exc}") from exc
    db.add(parked)
    await _commit(db)
    await db.refresh(parked)
    return parked


@router.get("/parked-calls", response_model=list[schemas.ParkedCallRead])
async def list_parked_calls(
    ctx: RequestContext = Depends(require_permission(READ)),
    db: AsyncSession = Depends(get_db),
) -> list[ParkedCall]:
    result = await db.execute(
        select(ParkedCall)
        .where(ParkedCall.tenant_id == ctx.tenant_id, ParkedCall.retrieved_at.is_(None))
        .order_by(ParkedCall.parked_at)
    )
    return list(result.scalars().all())


@router.post("/parked-calls/{parked_id}/retrieve", response_model=schemas.ParkedCallRead)
async def retrieve_parked_call(
    parked_id: uuid.UUID,
    payload: schemas.UnparkRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.CALLS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> ParkedCall:
    """Rings `agent_number`; on answer the agent joins the parked caller."""
    parked = await db.get(ParkedCall, parked_id)
    if parked is None or parked.tenant_id != ctx.tenant_id or parked.retrieved_at is not None:
        raise NotFoundError("Parked call not found")
    call = await db.get(Call, parked.call_id)
    caller_id = (call.to_number if call else None) or ""
    if not caller_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "No tenant number to present on the agent leg")
    credentials = await get_twilio_credentials(db, ctx.tenant_id)
    _twilio_credentials_or_400(credentials)
    try:
        place_outbound_call(
            to=payload.agent_number,
            from_=caller_id,
            voice_webhook_url=f"{svc.base_url()}/park/{parked.id}/join",
            credentials=credentials,
        )
    except (TwilioClientError, TwilioException) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Twilio could not ring the agent: {exc}") from exc
    parked.retrieved_at = datetime.now(UTC)
    await _commit(db)
    await db.refresh(parked)
    return parked


@router.post("/bridge/{call_id}")
async def masked_bridge_answer(
    call_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)
) -> Response:
    """Agent leg answered: dial the customer presenting the tenant's number."""
    call = await db.get(Call, call_id)
    if call is None or not call.to_number or not call.from_number:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Call not found")
    await _signed_form(request, db, call.tenant_id)
    if call.started_at is None:
        call.started_at = datetime.now(UTC)
        await db.commit()
    return _xml(
        ivr.build_masked_bridge_twiml(customer_number=call.to_number, caller_id=call.from_number)
    )


@router.post("/masked-bridge", response_model=schemas.MaskedBridgeResponse, status_code=201)
async def masked_bridge(
    payload: schemas.MaskedBridgeRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.CALLS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> schemas.MaskedBridgeResponse:
    """Rings the agent, then connects the customer using the tenant's caller id
    — the customer never sees the agent's number and vice versa."""
    decision = await svc.check_outbound_allowed(db, ctx.tenant_id, payload.customer_number)
    if not decision.allowed:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Customer cannot be called ({decision.reason})")

    caller_id = payload.caller_id
    if caller_id is None:
        numbers = (
            await db.execute(select(PhoneNumber).where(PhoneNumber.tenant_id == ctx.tenant_id))
        ).scalars().all()
        if len(numbers) != 1:
            raise HTTPException(422, "caller_id is required: tenant does not have exactly one number")
        caller_id = numbers[0].e164_number
    else:
        owned = (
            await db.execute(
                select(PhoneNumber.id).where(
                    PhoneNumber.tenant_id == ctx.tenant_id, PhoneNumber.e164_number == caller_id
                )
            )
        ).first()
        if not owned:
            raise HTTPException(422, "caller_id must be one of this tenant's phone numbers")

    customer, _ = await find_or_create_customer_by_phone(db, ctx.tenant_id, payload.customer_number)
    call = Call(
        tenant_id=ctx.tenant_id,
        customer_id=customer.id,
        direction=CallDirection.OUTBOUND,
        from_number=caller_id,
        to_number=payload.customer_number,
    )
    db.add(call)
    await db.flush()

    credentials = await get_twilio_credentials(db, ctx.tenant_id)
    _twilio_credentials_or_400(credentials)
    try:
        sid = place_outbound_call(
            to=payload.agent_number,
            from_=caller_id,
            voice_webhook_url=f"{svc.base_url()}/bridge/{call.id}",
            status_callback_url=(
                f"{get_settings().public_base_url}/api/v1/webhooks/telephony/twilio/status"
            ),
            credentials=credentials,
        )
    except (TwilioClientError, TwilioException) as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Twilio could not place the call: {exc}") from exc
    call.provider_call_sid = sid
    call.status = CallStatus.RINGING
    await db.commit()
    return schemas.MaskedBridgeResponse(call_id=call.id, provider_call_sid=sid, caller_id=caller_id)


# --- campaign dialer ---------------------------------------------------------------------------


async def _campaign(db: AsyncSession, tenant_id: uuid.UUID, campaign_id: uuid.UUID) -> Campaign:
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None or campaign.tenant_id != tenant_id:
        raise NotFoundError("Campaign not found")
    return campaign


def _settings_response(
    campaign_id: uuid.UUID, row: CampaignDialerSettings | None
) -> dict:
    base = schemas.DialerSettingsBase().model_dump()
    if row is None:
        return {"configured": False, "id": None, "campaign_id": campaign_id, **base}
    data = {key: getattr(row, key) for key in base}
    return {"configured": True, "id": row.id, "campaign_id": campaign_id, **data}


@router.get("/campaigns/{campaign_id}/dialer-settings")
async def get_dialer_settings(
    campaign_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_READ)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _campaign(db, ctx.tenant_id, campaign_id)
    row = await svc.get_dialer_settings(db, campaign_id)
    return _settings_response(campaign_id, row)


@router.put("/campaigns/{campaign_id}/dialer-settings")
async def put_dialer_settings(
    campaign_id: uuid.UUID,
    payload: schemas.DialerSettingsWrite,
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _campaign(db, ctx.tenant_id, campaign_id)
    row = await svc.get_dialer_settings(db, campaign_id)
    data = payload.model_dump()
    if row is None:
        row = CampaignDialerSettings(tenant_id=ctx.tenant_id, campaign_id=campaign_id, **data)
        db.add(row)
    else:
        for key, value in data.items():
            setattr(row, key, value)
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="campaign.dialer_settings_updated",
        resource_type="campaign",
        resource_id=campaign_id,
        extra={"mode": payload.mode},
    )
    await _commit(db)
    await db.refresh(row)
    return _settings_response(campaign_id, row)


@router.get("/campaigns/{campaign_id}/stats")
async def campaign_stats(
    campaign_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_READ)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    campaign = await _campaign(db, ctx.tenant_id, campaign_id)
    stats = await svc.campaign_stats(db, campaign)
    stats["status"] = campaign.status.value
    return stats


@router.get("/campaigns/{campaign_id}/outcomes", response_model=list[schemas.OutcomeRead])
async def list_outcomes(
    campaign_id: uuid.UUID,
    limit: int = Query(default=100, le=1000),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[CampaignOutcome]:
    await _campaign(db, ctx.tenant_id, campaign_id)
    result = await db.execute(
        select(CampaignOutcome)
        .where(CampaignOutcome.campaign_id == campaign_id, CampaignOutcome.tenant_id == ctx.tenant_id)
        .order_by(CampaignOutcome.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


@router.post(
    "/campaigns/{campaign_id}/contacts/{contact_id}/disposition",
    response_model=schemas.OutcomeRead,
    status_code=201,
)
async def set_disposition(
    campaign_id: uuid.UUID,
    contact_id: uuid.UUID,
    payload: schemas.DispositionCreate,
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> CampaignOutcome:
    """Agent-captured outcome. `callback` schedules a retry at callback_at,
    `qualified` routes the lead to the CRM (or a ticket), `dnc` adds the
    number to the DND registry."""
    campaign = await _campaign(db, ctx.tenant_id, campaign_id)
    contact = await db.get(CampaignContact, contact_id)
    if contact is None or contact.campaign_id != campaign.id or contact.tenant_id != ctx.tenant_id:
        raise NotFoundError("Campaign contact not found")
    if payload.call_id is not None:
        call = await db.get(Call, payload.call_id)
        if call is None or call.tenant_id != ctx.tenant_id:
            raise NotFoundError("Call not found")
    settings_row = await svc.get_dialer_settings(db, campaign_id)
    outcome = await svc.record_disposition(
        db,
        campaign,
        contact,
        disposition=payload.disposition,
        settings_row=settings_row,
        source="manual",
        notes=payload.notes,
        callback_at=payload.callback_at,
        qualified=payload.qualified,
        call_id=payload.call_id,
        user_id=ctx.user_id,
    )
    await _commit(db)
    await db.refresh(outcome)
    return outcome


@router.get("/campaigns/{campaign_id}/preview-next", response_model=schemas.PreviewContact | None)
async def preview_next(
    campaign_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_READ)),
    db: AsyncSession = Depends(get_db),
) -> schemas.PreviewContact | None:
    """Preview mode: the next contact an agent may dial right now (due, allowed
    by DND and inside the call window in the contact's timezone)."""
    campaign = await _campaign(db, ctx.tenant_id, campaign_id)
    settings_row = await svc.get_dialer_settings(db, campaign_id)
    now = datetime.now(UTC)
    for contact, state in await svc.eligible_contacts(db, campaign, now, limit=200):
        customer = await db.get(Customer, contact.customer_id)
        if customer is None or not customer.phone:
            continue
        zone = dialer.timezone_for_contact(
            customer.phone,
            extra=customer.extra,
            default=settings_row.default_timezone if settings_row else "Asia/Kolkata",
        )
        decision = await svc.check_outbound_allowed(
            db,
            ctx.tenant_id,
            customer.phone,
            now=now,
            tz_name=zone,
            window=(
                (settings_row.call_window_start, settings_row.call_window_end)
                if settings_row
                else ("09:00", "21:00")
            ),
            quiet_hours=(
                (settings_row.quiet_hours_start, settings_row.quiet_hours_end)
                if settings_row
                else ("21:00", "09:00")
            ),
        )
        if decision.allowed:
            return schemas.PreviewContact(
                contact_id=contact.id,
                customer_id=customer.id,
                name=customer.name,
                phone=customer.phone,
                timezone=zone,
                attempts=state.attempts if state else 0,
            )
    return None


def _csv_cell(value) -> str:
    text = "" if value is None else str(value)
    # Neutralise spreadsheet formula injection for free text (phones starting
    # with "+<digit>" are legitimate and left alone).
    if text and text[0] in "=@\t\r" or (
        text[:1] in "+-" and not text[1:2].isdigit()
    ):
        return "'" + text
    return text


@router.get("/campaigns/{campaign_id}/export.csv")
async def export_campaign_csv(
    campaign_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.CAMPAIGNS_READ)),
    db: AsyncSession = Depends(get_db),
) -> Response:
    campaign = await _campaign(db, ctx.tenant_id, campaign_id)
    rows = (
        await db.execute(
            select(CampaignContact, Customer)
            .join(Customer, Customer.id == CampaignContact.customer_id)
            .where(CampaignContact.campaign_id == campaign.id)
            .order_by(CampaignContact.created_at)
        )
    ).all()
    outcomes = (
        (
            await db.execute(
                select(CampaignOutcome)
                .where(CampaignOutcome.campaign_id == campaign.id)
                .order_by(CampaignOutcome.created_at)
            )
        )
        .scalars()
        .all()
    )
    last_outcome = {o.contact_id: o for o in outcomes}
    attempts = {o.contact_id: 0 for o in outcomes}
    for o in outcomes:
        attempts[o.contact_id] += 1

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "contact_id", "name", "phone", "email", "status", "attempts", "disposition",
            "qualified", "notes", "callback_at", "last_attempt_at", "lead_type", "lead_id",
        ]
    )  # fmt: skip
    for contact, customer in rows:
        outcome = last_outcome.get(contact.id)
        lead = (contact.result or {}).get("lead") or {}
        writer.writerow(
            [
                contact.id,
                _csv_cell(customer.name),
                customer.phone or "",
                _csv_cell(customer.email),
                contact.status.value,
                attempts.get(contact.id, 0),
                outcome.disposition if outcome else "",
                "yes" if outcome and outcome.qualified else "no",
                _csv_cell(outcome.notes if outcome else ""),
                outcome.callback_at.isoformat() if outcome and outcome.callback_at else "",
                contact.attempted_at.isoformat() if contact.attempted_at else "",
                lead.get("type", ""),
                lead.get("id", ""),
            ]
        )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="campaign-{campaign.id}.csv"'},
    )


@router.get("/meta")
async def telephony_plus_meta(
    ctx: RequestContext = Depends(require_permission(READ)),
) -> dict:
    """Static option lists the UI needs (dispositions, modes, IVR actions)."""
    return {
        "dispositions": svc.DISPOSITIONS,
        "dialer_modes": list(dialer.DIALER_MODES),
        "ivr_actions": list(ivr.IVR_ACTIONS),
        "queue_strategies": list(ivr.QUEUE_STRATEGIES),
        "ring_strategies": list(ivr.RING_STRATEGIES),
        "now": datetime.now(UTC).isoformat(),
        "default_quiet_hours": {"start": "21:00", "end": "09:00"},
    }

