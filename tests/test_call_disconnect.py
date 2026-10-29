"""A caller hanging up must end the call exactly once, and a call nobody ended must not stay live."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.integrations.twilio_client import CallNotLiveError, TwilioClientError
from app.services import call_finalize, call_reaper
from httpx import AsyncClient
from sqlalchemy import func, select

from packages.database.models import Call, CallDirection, CallEvent, CallStatus, Tenant
from packages.database.session import session_scope


async def _tenant(client: AsyncClient, slug: str) -> uuid.UUID:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"company_name": f"{slug} co", "tenant_slug": slug, "full_name": "Owner", "email": f"o@{slug}.com", "password": "supersecret1"},
    )
    assert resp.status_code == 201, resp.text
    async with session_scope() as db:
        return (await db.execute(select(Tenant.id).where(Tenant.slug == slug))).scalar_one()




async def _call(tenant_id, *, status, age_minutes, sid="CA123", started=True) -> Call:
    created = datetime.now(UTC) - timedelta(minutes=age_minutes)
    async with session_scope() as db:
        call = Call(
            tenant_id=tenant_id,
            direction=CallDirection.INBOUND,
            status=status,
            provider_call_sid=sid,
            created_at=created,
            started_at=created if started else None,
        )
        db.add(call)
        await db.commit()
        return call


async def _ended_events(call_id) -> int:
    async with session_scope() as db:
        return (
            await db.execute(select(func.count()).select_from(CallEvent).where(CallEvent.call_id == call_id, CallEvent.type == "CALL_ENDED"))
        ).scalar_one()


async def test_two_hangup_signals_at_once_finalize_the_call_only_once(client: AsyncClient):
    tenant_id = await _tenant(client, "hangup-race")
    call = await _call(tenant_id, status=CallStatus.IN_PROGRESS, age_minutes=2)

    both_loaded = asyncio.Barrier(2)  # both requests have read the call as live before either finishes it

    async def signal() -> bool:
        async with session_scope() as db:  # each signal arrives in its own request/session
            live = await db.get(Call, call.id)
            await both_loaded.wait()
            return await call_finalize.finalize_call(db, live, status=CallStatus.COMPLETED)

    results = await asyncio.gather(signal(), signal())
    assert sorted(results) == [False, True]
    assert await _ended_events(call.id) == 1


@pytest.fixture
def twilio(monkeypatch: pytest.MonkeyPatch):
    state = {"outcome": ("completed", 95), "error": None, "hung_up": []}

    def fetch(*, call_sid, credentials=None):
        if state["error"]:
            raise state["error"]
        return state["outcome"]

    monkeypatch.setattr(call_reaper, "fetch_call_outcome", fetch)
    monkeypatch.setattr(call_reaper, "redirect_call", lambda **kw: state["hung_up"].append(kw["call_sid"]))
    return state


async def _reap():
    async with session_scope() as db:
        return await call_reaper.reap_stuck_calls(db)


async def _status(call_id) -> Call:
    async with session_scope() as db:
        return await db.get(Call, call_id)


async def test_a_call_twilio_says_finished_is_closed_with_twilios_duration(client: AsyncClient, twilio):
    tenant_id = await _tenant(client, "reap-done")
    call = await _call(tenant_id, status=CallStatus.IN_PROGRESS, age_minutes=60)
    result = await _reap()
    assert (result.checked, result.finalized) == (1, 1)
    row = await _status(call.id)
    assert row.status == CallStatus.COMPLETED and row.ended_at is not None


async def test_recent_calls_are_left_alone(client: AsyncClient, twilio):
    tenant_id = await _tenant(client, "reap-recent")
    live = await _call(tenant_id, status=CallStatus.IN_PROGRESS, age_minutes=4)
    ringing = await _call(tenant_id, status=CallStatus.RINGING, age_minutes=1, sid="CA2", started=False)
    assert (await _reap()).checked == 0
    assert (await _status(live.id)).status == CallStatus.IN_PROGRESS
    assert (await _status(ringing.id)).status == CallStatus.RINGING


async def test_a_call_that_never_connected_is_failed_or_no_answer(client: AsyncClient, twilio):
    tenant_id = await _tenant(client, "reap-ring")
    twilio["outcome"] = ("no-answer", None)
    call = await _call(tenant_id, status=CallStatus.RINGING, age_minutes=20, started=False)
    await _reap()
    assert (await _status(call.id)).status == CallStatus.NO_ANSWER


async def test_a_call_still_running_past_its_limit_is_hung_up_and_closed(client: AsyncClient, twilio):
    tenant_id = await _tenant(client, "reap-live")
    twilio["outcome"] = ("in-progress", None)
    call = await _call(tenant_id, status=CallStatus.IN_PROGRESS, age_minutes=120)
    result = await _reap()
    assert result.still_live == 1 and twilio["hung_up"] == ["CA123"]
    assert (await _status(call.id)).status == CallStatus.COMPLETED


async def test_an_unknown_call_is_closed_by_whether_it_ever_started(client: AsyncClient, twilio):
    tenant_id = await _tenant(client, "reap-unknown")
    twilio["error"] = CallNotLiveError("gone")
    talked = await _call(tenant_id, status=CallStatus.IN_PROGRESS, age_minutes=90, sid="CA-a")
    never = await _call(tenant_id, status=CallStatus.QUEUED, age_minutes=90, sid="CA-b", started=False)
    await _reap()
    assert (await _status(talked.id)).status == CallStatus.COMPLETED
    assert (await _status(never.id)).status == CallStatus.FAILED


async def test_when_twilio_cannot_be_asked_the_call_is_kept_for_the_next_sweep(client: AsyncClient, twilio):
    tenant_id = await _tenant(client, "reap-retry")
    twilio["error"] = TwilioClientError("temporarily down")
    call = await _call(tenant_id, status=CallStatus.IN_PROGRESS, age_minutes=90)
    result = await _reap()
    assert result.finalized == 0
    assert (await _status(call.id)).status == CallStatus.IN_PROGRESS
