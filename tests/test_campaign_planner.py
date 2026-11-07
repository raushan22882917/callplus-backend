import json
from datetime import datetime
from zoneinfo import ZoneInfo

from app.services import campaign_planner
from app.services.agent_engine import mission_note
from app.services.call_analysis import parse_answers

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 4, 9, 0, tzinfo=IST)


def test_clean_plan_keeps_a_future_time_and_drops_a_past_one():
    raw = {
        "name": "Reminders",
        "goal": "Remind about tomorrow",
        "questions": ["Can you come?"],
        "audience": {"type": "all"},
        "when": "2026-11-05 10:00",
    }
    plan = campaign_planner.clean_plan(raw, IST, NOW)
    assert plan["scheduled_at"].startswith("2026-11-05T10:00")
    raw["when"] = "2026-11-03 10:00"
    assert campaign_planner.clean_plan(raw, IST, NOW)["scheduled_at"] is None


def test_clean_plan_rejects_nonsense_and_trims_lists():
    assert campaign_planner.clean_plan({"name": "", "goal": ""}, IST, NOW) is None
    assert (
        campaign_planner.clean_plan({"name": "x", "goal": "y", "when": "tomorrow-ish"}, IST, NOW)
        is None
    )
    plan = campaign_planner.clean_plan(
        {
            "name": "x",
            "goal": "y",
            "questions": [f"q{i}" for i in range(20)],
            "audience": {"type": "tag", "tags": []},
        },
        IST,
        NOW,
    )
    assert len(plan["questions"]) == 8 and plan["audience"] == {"type": "all"}


async def test_plan_turn_asks_then_proposes(monkeypatch):
    replies = iter(
        [
            {"ready": False, "reply": "Who should I call?"},
            {
                "ready": True,
                "reply": "Done.",
                "plan": {
                    "name": "Follow up",
                    "goal": "Check in",
                    "questions": ["Are you happy?"],
                    "audience": {"type": "all"},
                    "when": "now",
                },
            },
        ]
    )

    async def fake_chat(**kwargs):
        assert "Now: 2026-11-04" in kwargs["messages"][0]["content"]
        return {
            "choices": [{"message": {"content": "<think>x</think>" + json.dumps(next(replies))}}]
        }

    monkeypatch.setattr(campaign_planner, "chat_completion", fake_chat)
    kw = dict(timezone="Asia/Kolkata", tags=[], customer_count=3, api_key="k", now=NOW)
    first = await campaign_planner.plan_turn(
        messages=[{"role": "user", "content": "call people"}], **kw
    )
    assert first == {"ready": False, "reply": "Who should I call?"}
    second = await campaign_planner.plan_turn(
        messages=[{"role": "user", "content": "everyone"}], **kw
    )
    assert second["ready"] and second["plan"]["questions"] == ["Are you happy?"]


def test_parse_answers_keeps_only_real_answers():
    qs = ["Budget?", "When?"]
    out = parse_answers('{"Budget?": "50k", "When?": null, "Other": "x"}', qs)
    assert out == {"Budget?": "50k"}


def test_mission_note_lists_questions_and_is_empty_without_a_mission():
    assert mission_note(None) == ""
    note = mission_note({"goal": "Confirm Friday", "questions": ["Can you come?"]})
    assert "Confirm Friday" in note and "- Can you come?" in note


# ---- API: plan, approve, schedule, start --------------------------------------------------------

from app.api.v1 import campaigns as campaigns_api  # noqa: E402
from app.worker import campaign_tasks  # noqa: E402
from httpx import AsyncClient  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from packages.database.models import Campaign, CampaignContact, CampaignStatus  # noqa: E402
from tests.test_whatsapp_pipeline import _auth, _signup  # noqa: E402


async def _workspace_with_number(client: AsyncClient, slug: str) -> dict:
    tokens = await _signup(client, slug)
    agent = await client.post(
        "/api/v1/ai-agents", headers=_auth(tokens), json={"name": "Caller", "greeting": "Hi"}
    )
    await client.post(
        "/api/v1/phone-numbers",
        headers=_auth(tokens),
        json={"e164_number": "+919800011111", "ai_agent_id": agent.json()["id"]},
    )
    for n, phone in enumerate(["+919800000001", "+919800000002"]):
        r = await client.post(
            "/api/v1/customers", headers=_auth(tokens), json={"name": f"C{n}", "phone": phone}
        )
        assert r.status_code == 201, r.text
    return tokens


async def test_plan_endpoint_replies_without_creating_anything(client: AsyncClient, monkeypatch):
    tokens = await _workspace_with_number(client, "planco")

    async def fake_plan_turn(**kwargs):
        assert kwargs["customer_count"] == 2
        return {"ready": False, "reply": "When should I call?"}

    monkeypatch.setattr(campaigns_api.campaign_planner, "plan_turn", fake_plan_turn)
    resp = await client.post(
        "/api/v1/campaigns/plan",
        headers=_auth(tokens),
        json={"messages": [{"role": "user", "content": "call my customers"}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ready": False, "reply": "When should I call?", "plan": None}
    assert (await client.get("/api/v1/campaigns", headers=_auth(tokens))).json() == []


async def test_approved_plan_is_scheduled_then_started_when_due(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    tokens = await _workspace_with_number(client, "planco2")
    started: list[str] = []
    monkeypatch.setattr(campaign_tasks.run_campaign_task, "delay", lambda cid: started.append(cid))
    monkeypatch.setattr(campaigns_api.run_campaign_task, "delay", lambda cid: started.append(cid))

    plan = {
        "name": "Friday reminders",
        "goal": "Remind them about Friday",
        "questions": ["Can you come at 4 PM?"],
        "audience": {"type": "all"},
        "scheduled_at": "2099-01-01T10:00:00+05:30",
    }
    declared = await client.post("/api/v1/campaigns/a2p-declaration", headers=_auth(tokens))
    assert declared.status_code == 200, declared.text
    resp = await client.post(
        "/api/v1/campaigns/from-plan", headers=_auth(tokens), json={"plan": plan}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "scheduled" and body["questions"] == ["Can you come at 4 PM?"]
    assert started == []  # not before its time

    contacts = (
        await client.get(f"/api/v1/campaigns/{body['id']}/contacts", headers=_auth(tokens))
    ).json()
    assert len(contacts) == 2

    # The time arrives: the minute-by-minute check starts it.
    campaign = (
        await db_session.execute(select(Campaign).where(Campaign.name == "Friday reminders"))
    ).scalar_one()
    campaign.scheduled_at = campaign.scheduled_at.replace(year=2020)
    await db_session.commit()
    assert await campaign_tasks._start_due_campaigns() == 1
    assert started == [body["id"]]
    await db_session.refresh(campaign)
    assert campaign.status == CampaignStatus.RUNNING


async def test_plan_for_specific_numbers_creates_those_customers(client: AsyncClient, monkeypatch):
    tokens = await _workspace_with_number(client, "planco3")
    declared = await client.post("/api/v1/campaigns/a2p-declaration", headers=_auth(tokens))
    assert declared.status_code == 200, declared.text
    monkeypatch.setattr(campaigns_api.run_campaign_task, "delay", lambda cid: None)
    plan = {
        "name": "Two people",
        "goal": "Hello",
        "audience": {"type": "numbers", "numbers": ["9876543210"]},
    }
    resp = await client.post(
        "/api/v1/campaigns/from-plan", headers=_auth(tokens), json={"plan": plan}
    )
    assert resp.status_code == 201, resp.text
    contacts = (
        await client.get(f"/api/v1/campaigns/{resp.json()['id']}/contacts", headers=_auth(tokens))
    ).json()
    assert len(contacts) == 1


async def test_a_plan_without_a_calling_number_says_what_to_do(client: AsyncClient):
    tokens = await _signup(client, "planco4")
    await client.post(
        "/api/v1/customers", headers=_auth(tokens), json={"name": "A", "phone": "+919800000009"}
    )
    resp = await client.post(
        "/api/v1/campaigns/from-plan",
        headers=_auth(tokens),
        json={"plan": {"name": "x", "goal": "y"}},
    )
    assert resp.status_code == 422 and "phone number" in resp.json()["detail"].lower()
