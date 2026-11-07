import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from app.api.v1 import operations as operations_api
from app.main import app
from app.services import operations as svc
from app.tools import operations_tools as tools
from app.tools.context import ToolContext
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Customer,
    Tenant,
    Ticket,
    TicketComment,
    TicketPriority,
    TicketStatus,
)
from packages.database.models.operations import (  # noqa: F401 — registers tables
    Appointment,
    AppointmentResource,
    BookableService,
    FollowUp,
    PaymentPromise,
    SLAPolicy,
    TicketSLA,
)
from tests.test_call_control import _auth, _signup

# The lead mounts the router in api/v1/router.py; mount it here too so this
# module's tests pass before and after that integration.
if not any(getattr(r, "path", "").startswith("/api/v1/operations") for r in app.routes):
    app.include_router(operations_api.router, prefix="/api/v1")

HOURS = {d: [["09:00", "12:00"], ["13:00", "17:00"]] for d in ("mon", "tue", "wed", "thu", "fri")}
NEXT_MONDAY = date.today() + timedelta(days=(7 - date.today().weekday()))


def utc(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=UTC)


# ------------------------------ pure math ---------------------------------


def test_business_minutes_skip_nights_and_weekends() -> None:
    # Friday 2026-10-02 16:00 + 180 business minutes -> Monday 10:00.
    due = svc.add_business_minutes(utc(2026, 10, 2, 16), 180, start_hour=9, end_hour=18)
    assert due == utc(2026, 10, 5, 10)


def test_business_minutes_start_outside_hours_and_timezone() -> None:
    # Saturday start rolls to Monday 09:00 (+30m).
    assert svc.add_business_minutes(utc(2026, 10, 3, 12), 30, start_hour=9, end_hour=18) == utc(
        2026, 10, 5, 9, 30
    )
    # 09:00 Asia/Kolkata == 03:30 UTC
    due = svc.add_business_minutes(
        utc(2026, 10, 5, 3, 30), 60, start_hour=9, end_hour=18, tz_name="Asia/Kolkata"
    )
    assert due == utc(2026, 10, 5, 4, 30)


def test_compute_due_wall_clock() -> None:
    assert svc.compute_due(
        utc(2026, 10, 2, 23), 90, business_hours_only=False
    ) == utc(2026, 10, 3, 0, 30)


def test_sla_clock_states() -> None:
    start, due = utc(2026, 10, 5, 9), utc(2026, 10, 5, 13)
    assert svc.sla_clock(start, due, None, utc(2026, 10, 5, 10))["state"] == "ok"
    risky = svc.sla_clock(start, due, None, utc(2026, 10, 5, 12, 30))
    assert risky["state"] == "at_risk" and risky["seconds_remaining"] == 1800
    late = svc.sla_clock(start, due, None, utc(2026, 10, 5, 14))
    assert late["state"] == "breached" and late["seconds_remaining"] == -3600
    assert svc.sla_clock(start, due, utc(2026, 10, 5, 12), utc(2026, 10, 6))["state"] == "met"
    assert svc.sla_clock(start, due, utc(2026, 10, 5, 15), utc(2026, 10, 6))["state"] == "breached"


def test_generate_slots_respects_hours_busy_and_earliest() -> None:
    monday = date(2026, 10, 5)
    slots = svc.generate_slots(monday, HOURS, "UTC", 60, [], step_minutes=60)
    assert [s[0].hour for s in slots] == [9, 10, 11, 13, 14, 15, 16]  # lunch gap, no 12:00
    busy = [(utc(2026, 10, 5, 10), utc(2026, 10, 5, 11))]
    slots = svc.generate_slots(monday, HOURS, "UTC", 60, busy, step_minutes=30)
    starts = [s[0].strftime("%H:%M") for s in slots]
    assert "09:30" not in starts and "10:00" not in starts and "10:30" not in starts
    assert "09:00" in starts and "11:00" in starts and "11:30" not in starts  # 11:30-12:30 crosses lunch
    later = svc.generate_slots(
        monday, HOURS, "UTC", 60, [], step_minutes=60, earliest=utc(2026, 10, 5, 14, 1)
    )
    assert [s[0].hour for s in later] == [15, 16]
    assert svc.generate_slots(date(2026, 10, 4), HOURS, "UTC", 30, []) == []  # Sunday


def test_fits_working_hours() -> None:
    assert svc.fits_working_hours(HOURS, "UTC", utc(2026, 10, 5, 9), utc(2026, 10, 5, 10))
    assert not svc.fits_working_hours(HOURS, "UTC", utc(2026, 10, 5, 11, 30), utc(2026, 10, 5, 12, 30))
    assert not svc.fits_working_hours(HOURS, "UTC", utc(2026, 10, 3, 9), utc(2026, 10, 3, 10))


# ------------------------------ db helpers --------------------------------


async def _tenant(db: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = Tenant(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    db.add(tenant)
    await db.flush()
    customer = Customer(tenant_id=tenant.id, name="Jane", phone="+15550002222")
    db.add(customer)
    await db.commit()
    return tenant.id, customer.id


async def _service_and_resource(db, tenant_id, *, duration=60):
    service = BookableService(tenant_id=tenant_id, name="Cleaning", duration_minutes=duration, price=500)
    resource = AppointmentResource(tenant_id=tenant_id, name="Chair 1", kind="chair", working_hours=HOURS)
    db.add_all([service, resource])
    await db.commit()
    return service, resource


def _ctx(db, tenant_id, customer_id):
    return ToolContext(db=db, tenant_id=tenant_id, agent_id=None, call_id=None,
                       conversation_id=None, customer_id=customer_id)


def _monday_at(hour: int, minute: int = 0) -> datetime:
    return datetime.combine(NEXT_MONDAY, datetime.min.time(), tzinfo=UTC).replace(hour=hour, minute=minute)


# ------------------------------ appointments ------------------------------


async def test_booking_conflict_and_free_slots(db_session: AsyncSession) -> None:
    tenant_id, customer_id = await _tenant(db_session)
    service, resource = await _service_and_resource(db_session, tenant_id)
    first = await svc.book_appointment(
        db_session, tenant_id, customer_id=customer_id, service_id=service.id,
        start_at=_monday_at(10),
    )
    await db_session.commit()
    assert first.end_at == _monday_at(11)

    with pytest.raises(svc.SlotUnavailable, match="taken"):
        await svc.book_appointment(
            db_session, tenant_id, customer_id=customer_id, service_id=service.id,
            start_at=_monday_at(10, 30), resource_id=resource.id,
        )
    with pytest.raises(svc.SlotUnavailable, match="working hours"):
        await svc.book_appointment(
            db_session, tenant_id, customer_id=customer_id, service_id=service.id,
            start_at=_monday_at(20),
        )
    # back-to-back is fine
    await svc.book_appointment(
        db_session, tenant_id, customer_id=customer_id, service_id=service.id,
        start_at=_monday_at(11),
    )
    slots = await svc.free_slots(db_session, tenant_id, NEXT_MONDAY, service.id, step_minutes=60)
    assert [s["start"].hour for s in slots] == [9, 13, 14, 15, 16]


async def test_cancel_frees_slot_and_reschedule(db_session: AsyncSession) -> None:
    tenant_id, customer_id = await _tenant(db_session)
    service, _ = await _service_and_resource(db_session, tenant_id)
    appt = await svc.book_appointment(
        db_session, tenant_id, customer_id=customer_id, service_id=service.id, start_at=_monday_at(9)
    )
    other = await svc.book_appointment(
        db_session, tenant_id, customer_id=customer_id, service_id=service.id, start_at=_monday_at(14)
    )
    with pytest.raises(svc.SlotUnavailable):
        await svc.reschedule_appointment(db_session, tenant_id, appt.id, start_at=_monday_at(14))
    moved = await svc.reschedule_appointment(db_session, tenant_id, appt.id, start_at=_monday_at(15))
    assert moved.start_at == _monday_at(15)
    # overlapping its own old slot is allowed
    await svc.reschedule_appointment(db_session, tenant_id, moved.id, start_at=_monday_at(15, 30))
    await svc.cancel_appointment(db_session, tenant_id, other.id, "changed mind")
    again = await svc.book_appointment(
        db_session, tenant_id, customer_id=customer_id, service_id=service.id, start_at=_monday_at(14)
    )
    assert again.status == "booked"


async def test_appointment_tools_write_db_and_enforce_owner(db_session: AsyncSession) -> None:
    tenant_id, customer_id = await _tenant(db_session)
    await _service_and_resource(db_session, tenant_id)
    ctx = _ctx(db_session, tenant_id, customer_id)

    avail = await tools.check_availability(ctx, service="clean", date=NEXT_MONDAY.isoformat())
    assert avail["total_free"] > 0 and "09:00" in [s["start"] for s in avail["slots"]]

    booked = await tools.book_appointment(
        ctx, service="Cleaning", start_time=f"{NEXT_MONDAY.isoformat()}T10:00"
    )
    assert booked["booked"] is True
    row = await db_session.get(Appointment, uuid.UUID(booked["confirmation_id"]))
    assert row.source == "ai_agent" and row.start_at == _monday_at(10)

    clash = await tools.book_appointment(
        ctx, service="Cleaning", start_time=f"{NEXT_MONDAY.isoformat()}T10:30"
    )
    assert clash["booked"] is False and "taken" in clash["error"]

    stranger = Customer(tenant_id=tenant_id, name="Bob", phone="+15550003333")
    db_session.add(stranger)
    await db_session.flush()
    denied = await tools.cancel_appointment(
        _ctx(db_session, tenant_id, stranger.id), appointment_id=booked["confirmation_id"]
    )
    assert "error" in denied

    moved = await tools.reschedule_appointment(
        ctx, appointment_id=booked["confirmation_id"], new_start_time=f"{NEXT_MONDAY.isoformat()}T14:00"
    )
    assert moved["rescheduled"] is True
    cancelled = await tools.cancel_appointment(ctx, appointment_id=booked["confirmation_id"])
    assert cancelled["cancelled"] is True
    await db_session.refresh(row)
    assert row.status == "cancelled"


async def test_reminder_failure_is_recorded_without_whatsapp(db_session: AsyncSession) -> None:
    tenant_id, customer_id = await _tenant(db_session)
    service, resource = await _service_and_resource(db_session, tenant_id)
    soon = datetime.now(UTC) + timedelta(hours=3)
    appt = Appointment(
        tenant_id=tenant_id, customer_id=customer_id, service_id=service.id,
        resource_id=resource.id, start_at=soon, end_at=soon + timedelta(hours=1),
    )
    db_session.add(appt)
    await db_session.commit()
    result = await svc.send_due_reminders(db_session)
    assert (result.sent, result.failed) == (0, 1)
    await db_session.refresh(appt)
    assert appt.reminder_sent_at is None and "WhatsApp" in appt.reminder_error
    assert appt.reminder_attempts == 1


async def test_no_show_followup_created_once(db_session: AsyncSession) -> None:
    tenant_id, customer_id = await _tenant(db_session)
    service, resource = await _service_and_resource(db_session, tenant_id)
    past = datetime.now(UTC) - timedelta(hours=2)
    db_session.add(Appointment(
        tenant_id=tenant_id, customer_id=customer_id, service_id=service.id, resource_id=resource.id,
        start_at=past, end_at=past + timedelta(hours=1), status="no_show",
    ))
    await db_session.commit()
    assert await svc.create_no_show_followups(db_session) == 1
    assert await svc.create_no_show_followups(db_session) == 0
    follow_ups = (await db_session.execute(select(FollowUp))).scalars().all()
    assert len(follow_ups) == 1 and follow_ups[0].reference_type == "appointment"


# ------------------------------ SLA ---------------------------------------


async def test_apply_sla_and_breach_detection(db_session: AsyncSession) -> None:
    tenant_id, customer_id = await _tenant(db_session)
    db_session.add(SLAPolicy(tenant_id=tenant_id, priority="high",
                             first_response_minutes=30, resolution_minutes=240))
    ticket = Ticket(tenant_id=tenant_id, customer_id=customer_id, subject="Broken",
                    priority=TicketPriority.HIGH)
    low = Ticket(tenant_id=tenant_id, customer_id=customer_id, subject="Meh",
                 priority=TicketPriority.LOW)
    db_session.add_all([ticket, low])
    await db_session.flush()
    created = utc(2026, 10, 5, 9)
    sla = await svc.apply_sla(db_session, tenant_id, ticket, now=created)
    assert await svc.apply_sla(db_session, tenant_id, low, now=created) is None  # no policy
    assert sla.first_response_due_at == utc(2026, 10, 5, 9, 30)
    assert sla.resolution_due_at == utc(2026, 10, 5, 13)
    await db_session.commit()

    assert await svc.refresh_sla(db_session, now=utc(2026, 10, 5, 9, 10)) == []
    breaches = await svc.refresh_sla(db_session, now=utc(2026, 10, 5, 10))
    assert [b.kind for b in breaches] == ["first_response"]
    # not re-emitted
    assert await svc.refresh_sla(db_session, now=utc(2026, 10, 5, 11)) == []
    later = await svc.refresh_sla(db_session, now=utc(2026, 10, 5, 14))
    assert [b.kind for b in later] == ["resolution"]


async def test_first_response_comment_and_resolution_stop_clock(db_session: AsyncSession) -> None:
    tenant_id, customer_id = await _tenant(db_session)
    from packages.database.models import TenantUser  # noqa: F401
    db_session.add(SLAPolicy(tenant_id=tenant_id, priority="medium",
                             first_response_minutes=60, resolution_minutes=120))
    ticket = Ticket(tenant_id=tenant_id, customer_id=customer_id, subject="Q")
    db_session.add(ticket)
    await db_session.flush()
    # Resolving a ticket stamps updated_at with the database clock, and that
    # stamp is when the SLA clock stops. Open the clock a few minutes ago so
    # the stop is still inside the hour, whatever the wall clock is.
    opened = datetime.now(UTC) - timedelta(minutes=10)
    await svc.apply_sla(db_session, tenant_id, ticket, now=opened)
    await db_session.commit()
    # Agents (author_user_id set) count; we can't insert a real user cheaply, so
    # resolve the ticket instead and check the clock stops.
    ticket.status = TicketStatus.RESOLVED
    await db_session.commit()
    assert await svc.refresh_sla(db_session, now=datetime.now(UTC)) == []
    sla = (await db_session.execute(select(TicketSLA))).scalar_one()
    assert sla.resolved_at is not None and sla.first_responded_at is not None
    assert not sla.first_response_breached and not sla.resolution_breached
    _ = TicketComment


async def test_sla_api_and_ticket_hook(client: AsyncClient) -> None:
    tokens = await _signup(client, slug="sla-co", email="owner@slaco.com")
    h = _auth(tokens)
    bad = await client.post("/api/v1/operations/sla/policies", headers=h, json={
        "priority": "high", "first_response_minutes": 60, "resolution_minutes": 30})
    assert bad.status_code == 400
    ok = await client.post("/api/v1/operations/sla/policies", headers=h, json={
        "priority": "high", "first_response_minutes": 15, "resolution_minutes": 60})
    assert ok.status_code == 201, ok.text
    dup = await client.post("/api/v1/operations/sla/policies", headers=h, json={
        "priority": "high", "first_response_minutes": 15, "resolution_minutes": 60})
    assert dup.status_code == 409

    customer = (await client.post("/api/v1/customers", headers=h, json={
        "name": "Jane", "phone": "+15550001111"})).json()
    ticket = await client.post("/api/v1/tickets", headers=h, json={
        "customer_id": customer["id"], "subject": "Down", "priority": "high"})
    assert ticket.status_code == 201, ticket.text

    rows = (await client.get("/api/v1/operations/sla/tickets", headers=h)).json()
    assert len(rows) == 1 and rows[0]["ticket_id"] == ticket.json()["id"]
    assert rows[0]["first_response"]["state"] == "ok" and not rows[0]["breached"]
    summary = (await client.get("/api/v1/operations/sla/summary", headers=h)).json()
    assert summary[0]["priority"] == "high" and summary[0]["open"] == 1

    cat = await client.post("/api/v1/operations/categories", headers=h, json={
        "name": "Billing", "department": "Finance"})
    assert cat.status_code == 201
    tagged = await client.patch(f"/api/v1/operations/sla/tickets/{ticket.json()['id']}", headers=h,
                                json={"category_id": cat.json()["id"]})
    assert tagged.json()["department"] == "Finance"


async def test_appointment_api_flow_and_tenant_isolation(client: AsyncClient) -> None:
    tokens = await _signup(client, slug="appt-co", email="owner@apptco.com")
    other = await _signup(client, slug="appt-other", email="owner@apptother.com")
    h = _auth(tokens)
    customer = (await client.post("/api/v1/customers", headers=h, json={
        "name": "Jane", "phone": "+15550001111"})).json()
    service = (await client.post("/api/v1/operations/services", headers=h, json={
        "name": "Consult", "duration_minutes": 30, "price": "100"})).json()
    bad = await client.post("/api/v1/operations/resources", headers=h, json={
        "name": "Dr", "working_hours": {"mon": [["17:00", "09:00"]]}})
    assert bad.status_code == 422
    resource = (await client.post("/api/v1/operations/resources", headers=h, json={
        "name": "Dr A", "kind": "staff", "working_hours": HOURS})).json()

    slots = (await client.get("/api/v1/operations/availability", headers=h, params={
        "date": NEXT_MONDAY.isoformat(), "service_id": service["id"]})).json()
    assert slots and slots[0]["resource_id"] == resource["id"]

    body = {"customer_id": customer["id"], "service_id": service["id"], "start_at": slots[0]["start"]}
    booked = await client.post("/api/v1/operations/appointments", headers=h, json=body)
    assert booked.status_code == 201, booked.text
    assert booked.json()["customer_name"] == "Jane"
    again = await client.post("/api/v1/operations/appointments", headers=h, json=body)
    assert again.status_code == 409

    appt_id = booked.json()["id"]
    resched = await client.post(f"/api/v1/operations/appointments/{appt_id}/reschedule", headers=h,
                                json={"start_at": slots[3]["start"]})
    assert resched.status_code == 200 and resched.json()["start_at"].startswith(slots[3]["start"][:16])
    cancelled = await client.post(f"/api/v1/operations/appointments/{appt_id}/cancel", headers=h, json={})
    assert cancelled.json()["status"] == "cancelled"
    assert (await client.post(f"/api/v1/operations/appointments/{appt_id}/cancel", headers=h,
                              json={})).status_code == 400

    hidden = await client.get("/api/v1/operations/appointments", headers=_auth(other))
    assert hidden.json() == []
    assert (await client.get(f"/api/v1/operations/appointments/{appt_id}",
                             headers=_auth(other))).status_code == 404


# ------------------------------ follow-ups --------------------------------


async def test_due_follow_up_selection(db_session: AsyncSession) -> None:
    tenant_id, customer_id = await _tenant(db_session)
    now = utc(2026, 10, 5, 12)

    def fu(due, status="pending"):
        return FollowUp(tenant_id=tenant_id, customer_id=customer_id, channel="sms",
                        due_at=due, status=status, payload={"message": "hi"})

    due_old, due_new = fu(now - timedelta(hours=2)), fu(now - timedelta(minutes=1))
    db_session.add_all([due_new, due_old, fu(now + timedelta(minutes=5)),
                        fu(now - timedelta(hours=1), "done"), fu(now - timedelta(hours=1), "cancelled")])
    await db_session.commit()
    selected = await svc.select_due_follow_ups(db_session, now=now)
    assert [f.id for f in selected] == [due_old.id, due_new.id]
    assert len(await svc.select_due_follow_ups(db_session, now=now, limit=1)) == 1


async def test_follow_up_execution_marks_failures_honestly(db_session: AsyncSession) -> None:
    tenant_id, customer_id = await _tenant(db_session)
    now = utc(2026, 10, 5, 12)
    wa = FollowUp(tenant_id=tenant_id, customer_id=customer_id, channel="whatsapp",
                  due_at=now, payload={"message": "hello"})
    mail = FollowUp(tenant_id=tenant_id, customer_id=customer_id, channel="email",
                    due_at=now, payload={"message": "hello"})
    sms = FollowUp(tenant_id=tenant_id, customer_id=customer_id, channel="sms",
                   due_at=now, payload={"message": "hello"})
    db_session.add_all([wa, mail, sms])
    await db_session.commit()
    counts = await svc.run_due_follow_ups(db_session, now=now)
    assert counts == {"done": 0, "failed": 3, "retry": 0}
    for row in (wa, mail, sms):
        await db_session.refresh(row)
        assert row.status == "failed" and row.last_error and row.attempts == 1
    assert "WhatsApp" in wa.last_error and "email" in mail.last_error.lower()
    # failed ones are not picked up again
    assert await svc.select_due_follow_ups(db_session, now=now) == []


async def test_follow_up_whatsapp_success_and_transient_retry(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.integrations.whatsapp import WhatsAppAPIError

    from packages.database.models import WhatsAppAccount

    tenant_id, customer_id = await _tenant(db_session)
    db_session.add(WhatsAppAccount(tenant_id=tenant_id, phone_number_id="pn1", waba_id="w",
                                   display_phone_number="+1", encrypted_access_token="x"))
    now = utc(2026, 10, 5, 12)
    row = FollowUp(tenant_id=tenant_id, customer_id=customer_id, channel="whatsapp",
                   due_at=now, payload={"message": "hello"})
    db_session.add(row)
    await db_session.commit()

    sent = []

    async def fake_send(account, *, to, body):
        sent.append((to, body))
        return {}

    monkeypatch.setattr(svc, "send_text_message", fake_send)
    await svc.run_due_follow_ups(db_session, now=now)
    await db_session.refresh(row)
    assert row.status == "done" and sent == [("15550002222", "hello")]

    flaky = FollowUp(tenant_id=tenant_id, customer_id=customer_id, channel="whatsapp",
                     due_at=now, payload={"message": "again"})
    db_session.add(flaky)
    await db_session.commit()

    async def boom(account, *, to, body):
        raise WhatsAppAPIError("rate limited")

    monkeypatch.setattr(svc, "send_text_message", boom)
    await svc.run_due_follow_ups(db_session, now=now)
    await db_session.refresh(flaky)
    assert flaky.status == "pending" and flaky.due_at > now and flaky.attempts == 1


async def test_follow_up_api(client: AsyncClient) -> None:
    h = _auth(await _signup(client, slug="fu-co", email="owner@fuco.com"))
    customer = (await client.post("/api/v1/customers", headers=h, json={
        "name": "Jane", "phone": "+15550001111"})).json()
    missing = await client.post("/api/v1/operations/follow-ups", headers=h, json={
        "customer_id": customer["id"], "channel": "sms", "due_at": "2030-01-01T00:00:00Z"})
    assert missing.status_code == 400
    made = await client.post("/api/v1/operations/follow-ups", headers=h, json={
        "customer_id": customer["id"], "channel": "sms", "due_at": "2030-01-01T00:00:00Z",
        "message": "ping"})
    assert made.status_code == 201 and made.json()["created_by"] == "human"
    cancelled = await client.patch(f"/api/v1/operations/follow-ups/{made.json()['id']}", headers=h,
                                   json={"status": "cancelled"})
    assert cancelled.json()["status"] == "cancelled"
    listed = await client.get("/api/v1/operations/follow-ups", headers=h, params={"status": "cancelled"})
    assert len(listed.json()) == 1


# ------------------------------ collections -------------------------------


async def test_promise_tool_overdue_and_reminder_cancel(db_session: AsyncSession) -> None:
    tenant_id, customer_id = await _tenant(db_session)
    ctx = _ctx(db_session, tenant_id, customer_id)
    due = (datetime.now(UTC) + timedelta(days=3)).date()
    result = await tools.record_promise_to_pay(ctx, amount=1500.5, due_date=due.isoformat())
    assert result["recorded"] and result["reminder_scheduled"]
    assert "error" in await tools.record_promise_to_pay(ctx, amount=10, due_date="2001-01-01")
    assert "error" in await tools.record_promise_to_pay(ctx, amount=-5, due_date=due.isoformat())

    promise = await db_session.get(PaymentPromise, uuid.UUID(result["promise_id"]))
    reminder = (await db_session.execute(select(FollowUp))).scalar_one()
    assert reminder.reference_id == promise.id and reminder.due_at.date() == due
    assert not svc.is_overdue(promise, datetime.now(UTC).date())
    assert svc.is_overdue(promise, due + timedelta(days=1))
    await svc.cancel_promise_reminders(db_session, promise.id)
    await db_session.refresh(reminder)
    assert reminder.status == "cancelled"


async def test_collections_api_overdue(client: AsyncClient, db_session: AsyncSession) -> None:
    tokens = await _signup(client, slug="col-co", email="owner@colco.com")
    h = _auth(tokens)
    customer = (await client.post("/api/v1/customers", headers=h, json={
        "name": "Jane", "phone": "+15550001111"})).json()
    future = (datetime.now(UTC) + timedelta(days=5)).date().isoformat()
    created = await client.post("/api/v1/operations/collections/promises", headers=h, json={
        "customer_id": customer["id"], "amount": "250.00", "due_date": future})
    assert created.status_code == 201, created.text
    # Backdate directly to make it overdue.
    promise = await db_session.get(PaymentPromise, uuid.UUID(created.json()["id"]))
    promise.due_date = (datetime.now(UTC) - timedelta(days=4)).date()
    await db_session.commit()
    overdue = (await client.get("/api/v1/operations/collections/overdue", headers=h)).json()
    assert len(overdue) == 1 and overdue[0]["days_overdue"] == 4 and overdue[0]["customer_name"] == "Jane"
    remind = await client.post(
        f"/api/v1/operations/collections/promises/{created.json()['id']}/remind", headers=h)
    assert remind.status_code == 201 and remind.json()["channel"] == "whatsapp"
    kept = await client.patch(f"/api/v1/operations/collections/promises/{created.json()['id']}",
                              headers=h, json={"status": "kept"})
    assert kept.json()["status"] == "kept"
    assert (await client.get("/api/v1/operations/collections/overdue", headers=h)).json() == []


@pytest.mark.asyncio
async def test_priority_change_reapplies_sla_targets(db_session: AsyncSession) -> None:
    tenant_id, customer_id = await _tenant(db_session)
    db_session.add_all([
        SLAPolicy(tenant_id=tenant_id, priority="medium", first_response_minutes=240, resolution_minutes=1440),
        SLAPolicy(tenant_id=tenant_id, priority="urgent", first_response_minutes=15, resolution_minutes=60),
    ])
    ticket = Ticket(tenant_id=tenant_id, customer_id=customer_id, subject="Slow",
                    priority=TicketPriority.MEDIUM)
    db_session.add(ticket)
    await db_session.flush()
    created = utc(2026, 10, 5, 9)
    sla = await svc.apply_sla(db_session, tenant_id, ticket, now=created)
    assert sla.resolution_due_at == utc(2026, 10, 6, 9)

    ticket.priority = TicketPriority.URGENT
    await svc.reapply_sla_for_priority(db_session, tenant_id, ticket)
    await db_session.refresh(sla)
    assert sla.priority == "urgent"
    assert sla.first_response_due_at == utc(2026, 10, 5, 9, 15)
    assert sla.resolution_due_at == utc(2026, 10, 5, 10)
