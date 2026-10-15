import uuid
from datetime import UTC, datetime

from app.api.v1.crm import router as crm_router
from app.main import app
from app.services.crm import ScoreSignals, compute_score
from app.tools.context import ToolContext
from app.tools.crm_tools import create_lead, schedule_follow_up
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Customer, Tenant
from packages.database.models import crm as _crm_models  # noqa: F401
from packages.database.models.call import Call, CallDirection, CallStatus
from packages.database.models.crm import CrmStage

if not any(getattr(r, "path", "").startswith("/api/v1/crm") for r in app.routes):
    app.include_router(crm_router, prefix="/api/v1")


async def _signup(client: AsyncClient, slug: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "company_name": slug.title(),
            "tenant_slug": slug,
            "full_name": "Owner",
            "email": f"owner@{slug}.com",
            "password": "supersecret1",
        },
    )
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# ------------------------------------------------------------------ scoring


def test_score_won_and_lost_are_terminal() -> None:
    assert compute_score(ScoreSignals(stage=CrmStage.WON)).score == 100
    assert compute_score(ScoreSignals(stage=CrmStage.LOST)).score == 0


def test_score_rewards_positive_buying_calls_and_source() -> None:
    cold = compute_score(ScoreSignals())
    warm = compute_score(
        ScoreSignals(
            stage=CrmStage.QUALIFIED,
            source="referral",
            has_email=True,
            has_phone=True,
            calls=[("positive", "interested", "wants pricing for the pro plan")],
            total_calls=1,
        )
    )
    assert warm.score > cold.score
    rules = {i["rule"] for i in warm.breakdown}
    assert {"source", "calls", "engagement", "stage"} <= rules
    assert 0 <= warm.score <= 100


def test_score_penalises_negative_calls_and_is_clamped() -> None:
    bad = compute_score(
        ScoreSignals(calls=[("negative", "not_interested", None)] * 5, total_calls=5)
    )
    assert bad.score >= 0
    assert bad.score < compute_score(ScoreSignals()).score + 15
    huge = compute_score(
        ScoreSignals(
            stage=CrmStage.NEGOTIATION,
            source="referral",
            has_email=True,
            has_phone=True,
            calls=[("positive", "sale", "buy now")] * 5,
            total_calls=9,
            notes=9,
            tasks_done=9,
        )
    )
    assert huge.score <= 100


async def test_rescore_endpoint_uses_analyzed_calls(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _signup(client, "scoring")
    me = await client.get("/api/v1/auth/me", headers=headers)
    tenant_id = uuid.UUID(me.json()["tenant"]["id"]) if "tenant" in me.json() else None
    lead = (
        await client.post(
            "/api/v1/crm/leads",
            headers=headers,
            json={"name": "Priya", "phone": "+919800000001", "source": "website"},
        )
    ).json()
    before = lead["score"]
    if tenant_id is None:
        from sqlalchemy import select

        tenant_id = (await db_session.execute(select(Tenant.id).where(Tenant.slug == "scoring"))).scalar_one()
    db_session.add(
        Call(
            tenant_id=tenant_id,
            direction=CallDirection.INBOUND,
            status=CallStatus.COMPLETED,
            from_number="+919800000001",
            to_number="+911100000000",
            sentiment="positive",
            outcome="interested",
            intent="wants to buy the premium plan",
            analyzed_at=datetime.now(UTC),
        )
    )
    await db_session.commit()

    resp = await client.post(f"/api/v1/crm/leads/{lead['id']}/rescore", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["score"] > before
    assert any(i["rule"] == "calls" for i in resp.json()["score_breakdown"])

    timeline = await client.get(f"/api/v1/crm/leads/{lead['id']}/timeline", headers=headers)
    assert [i["type"] for i in timeline.json()] == ["call"]

    all_resp = await client.post("/api/v1/crm/leads/rescore", headers=headers)
    assert all_resp.json() == {"rescored": 1}


# ----------------------------------------------------------------- pipeline


async def test_pipeline_grouping_and_move(client: AsyncClient) -> None:
    headers = await _signup(client, "pipe")
    ids = {}
    for name, stage, value in [
        ("A", "new_lead", 100),
        ("B", "new_lead", 250.5),
        ("C", "qualified", 1000),
    ]:
        r = await client.post(
            "/api/v1/crm/leads",
            headers=headers,
            json={"name": name, "phone": f"+9198{len(ids)}00000", "stage": stage, "value": value},
        )
        assert r.status_code == 201, r.text
        ids[name] = r.json()["id"]

    board = (await client.get("/api/v1/crm/pipeline", headers=headers)).json()
    assert [s["stage"] for s in board["stages"]] == [
        "new_lead", "contacted", "qualified", "demo", "proposal", "negotiation", "won", "lost",
    ]
    by_stage = {s["stage"]: s for s in board["stages"]}
    assert by_stage["new_lead"]["count"] == 2
    assert float(by_stage["new_lead"]["total_value"]) == 350.5
    assert by_stage["qualified"]["count"] == 1
    assert by_stage["won"]["count"] == 0 and by_stage["won"]["leads"] == []
    assert board["total_leads"] == 3

    moved = await client.post(
        f"/api/v1/crm/leads/{ids['A']}/move",
        headers=headers,
        json={"stage": "lost", "lost_reason": "Went with competitor"},
    )
    assert moved.status_code == 200
    assert moved.json()["stage"] == "lost"
    assert moved.json()["score"] == 0

    board = (await client.get("/api/v1/crm/pipeline", headers=headers)).json()
    by_stage = {s["stage"]: s for s in board["stages"]}
    assert by_stage["new_lead"]["count"] == 1
    assert by_stage["lost"]["count"] == 1

    timeline = (await client.get(f"/api/v1/crm/leads/{ids['A']}/timeline", headers=headers)).json()
    assert timeline[0]["type"] == "stage_change"
    assert "Went with competitor" in timeline[0]["body"]


# --------------------------------------------------------------- CSV import


async def test_csv_import_validates_and_dedupes(client: AsyncClient) -> None:
    headers = await _signup(client, "csvco")
    await client.post(
        "/api/v1/crm/leads", headers=headers, json={"name": "Existing", "email": "dup@x.com"}
    )
    csv_body = (
        "name,email,phone,company,source,tags,stage,value\n"
        "Good One,good@x.com,+919811111111,Acme,expo,vip;hot,qualified,5000\n"
        ",nobody@x.com,,,,,,\n"
        "Bad Email,not-an-email,,,,,,\n"
        "Dup Email,DUP@x.com,,,,,,\n"
        "Bad Stage,s@x.com,,,,,galaxy,\n"
        "Phone Only,,+919822222222,Acme,,,,\n"
        "Phone Dupe,,+91 98222 22222,,,,,\n"
        "No Contact,,,,,,,\n"
    )
    resp = await client.post(
        "/api/v1/crm/leads/import",
        headers=headers,
        files={"file": ("leads.csv", csv_body, "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] == 2
    reasons = {s["row"]: s["reason"] for s in body["skipped"]}
    assert reasons[3] == "Missing name"
    assert reasons[4].startswith("Invalid email")
    assert reasons[5].startswith("Duplicate email")
    assert reasons[6].startswith("Unknown stage")
    assert reasons[8].startswith("Duplicate phone")
    assert reasons[9] == "Needs an email or phone"

    leads = (await client.get("/api/v1/crm/leads?source=expo", headers=headers)).json()
    assert len(leads) == 1
    assert leads[0]["stage"] == "qualified" and leads[0]["tags"] == ["vip", "hot"]
    companies = (await client.get("/api/v1/crm/companies", headers=headers)).json()
    assert [c["name"] for c in companies] == ["Acme"]  # shared, not duplicated

    no_name = await client.post(
        "/api/v1/crm/leads/import",
        headers=headers,
        files={"file": ("x.csv", "email\na@b.com\n", "text/csv")},
    )
    assert no_name.status_code == 422


# ----------------------------------------------------------- tenant isolation


async def test_tenant_isolation(client: AsyncClient) -> None:
    a = await _signup(client, "iso-a")
    b = await _signup(client, "iso-b")
    lead = (await client.post("/api/v1/crm/leads", headers=a, json={"name": "Secret"})).json()
    deal = (await client.post("/api/v1/crm/deals", headers=a, json={"title": "Big", "value": 10})).json()
    task = (await client.post("/api/v1/crm/tasks", headers=a, json={"title": "Call", "lead_id": lead["id"]})).json()

    assert (await client.get("/api/v1/crm/leads", headers=b)).json() == []
    assert (await client.get("/api/v1/crm/deals", headers=b)).json() == []
    assert (await client.get("/api/v1/crm/tasks", headers=b)).json() == []
    pipe = (await client.get("/api/v1/crm/pipeline", headers=b)).json()
    assert pipe["total_leads"] == 0

    for path in (f"leads/{lead['id']}", f"deals/{deal['id']}", f"tasks/{task['id']}"):
        assert (await client.get(f"/api/v1/crm/{path}", headers=b)).status_code == 404
        assert (await client.delete(f"/api/v1/crm/{path}", headers=b)).status_code == 404
    assert (
        await client.post(f"/api/v1/crm/leads/{lead['id']}/move", headers=b, json={"stage": "won"})
    ).status_code == 404
    assert (await client.get(f"/api/v1/crm/leads/{lead['id']}/timeline", headers=b)).status_code == 404

    # B cannot attach its records to A's lead.
    cross = await client.post(
        "/api/v1/crm/tasks", headers=b, json={"title": "x", "lead_id": lead["id"]}
    )
    assert cross.status_code == 404
    cross_note = await client.post(
        "/api/v1/crm/notes", headers=b, json={"body": "x", "lead_id": lead["id"]}
    )
    assert cross_note.status_code == 404

    assert (await client.get(f"/api/v1/crm/leads/{lead['id']}", headers=a)).status_code == 200


async def test_tasks_due_filters(client: AsyncClient) -> None:
    h = await _signup(client, "tasks")
    now = datetime.now(UTC)
    await client.post(
        "/api/v1/crm/tasks",
        headers=h,
        json={"title": "late", "due_at": "2020-01-01T00:00:00+00:00", "type": "call"},
    )
    await client.post(
        "/api/v1/crm/tasks", headers=h, json={"title": "today", "due_at": now.replace(hour=23, minute=59).isoformat()}
    )
    overdue = (await client.get("/api/v1/crm/tasks?due=overdue", headers=h)).json()
    assert [t["title"] for t in overdue] == ["late"]
    today = (await client.get("/api/v1/crm/tasks?due=today", headers=h)).json()
    assert [t["title"] for t in today] == ["today"]
    done = await client.patch(
        f"/api/v1/crm/tasks/{overdue[0]['id']}", headers=h, json={"status": "done"}
    )
    assert done.json()["completed_at"] is not None
    assert (await client.get("/api/v1/crm/tasks?due=overdue", headers=h)).json() == []


# --------------------------------------------------------------------- tools


async def test_ai_tools_create_lead_and_follow_up(db_session: AsyncSession) -> None:
    tenant = Tenant(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    other = Tenant(name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
    db_session.add_all([tenant, other])
    await db_session.flush()
    customer = Customer(tenant_id=tenant.id, name="Jane", phone="+15550002222")
    db_session.add(customer)
    await db_session.commit()

    ctx = ToolContext(
        db=db_session, tenant_id=tenant.id, agent_id=None, call_id=None,
        conversation_id=None, customer_id=customer.id,
    )
    first = await create_lead(ctx, interest="wants the enterprise plan")
    assert first["created"] is True
    again = await create_lead(ctx)
    assert again["created"] is False and again["lead_id"] == first["lead_id"]

    task = await schedule_follow_up(ctx, title="Call Jane back", due_in_hours=2, task_type="call")
    assert task["lead_id"] == first["lead_id"]
    assert "error" in await schedule_follow_up(ctx, title="x", task_type="fax")

    # A different tenant's context never sees or attaches to this lead.
    foreign = ToolContext(
        db=db_session, tenant_id=other.id, agent_id=None, call_id=None,
        conversation_id=None, customer_id=None,
    )
    assert "error" in await schedule_follow_up(foreign, title="x", lead_id=first["lead_id"])
    fresh = await create_lead(foreign, name="Bob", phone="+15550002222")
    assert fresh["created"] is True and fresh["lead_id"] != first["lead_id"]
