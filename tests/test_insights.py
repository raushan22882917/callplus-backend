from datetime import UTC, datetime, timedelta

import pytest
from app.api.v1 import insights as insights_api
from app.main import app
from app.services import copilot, cx_analytics, forecast, ops_analytics, qa, transcript_search
from app.services.cx_analytics import CallPoint

from packages.database import models  # noqa: F401
from packages.database.models import Call, CallDirection, CallStatus
from packages.database.models.insights import QAScorecard  # noqa: F401

if not any(getattr(r, "path", "").startswith("/api/v1/insights") for r in app.routes):
    app.include_router(insights_api.router, prefix="/api/v1")  # until the lead registers it

T0 = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)


def turn(speaker, text, seconds=0):
    return qa.Turn(speaker, text, T0 + timedelta(seconds=seconds))


# ---------------------------------------------------------------- QA parsing


def test_parse_qa_clamps_and_strips_reasoning():
    text = (
        '<think>x</think>```json\n{"compliance": 140, "communication": "85.4", "resolution": -5, '
        '"sales": null, "summary": "Good.", "problems": [{"severity": "SEVERE", "message": "Rude", '
        '"evidence": "no"}, "Too slow", {"message": ""}]}\n```'
    )
    r = qa.parse_qa(text)
    assert (r["compliance"], r["communication"], r["resolution"], r["sales"]) == (100, 85, 0, None)
    assert [p["severity"] for p in r["problems"]] == ["minor", "minor"]
    assert r["problems"][1]["message"] == "Too slow"


def test_parse_qa_rejects_garbage():
    assert qa.parse_qa("nope") is None
    assert qa.parse_qa("[1]") is None
    assert qa.parse_qa('{"summary": "x"}') is None


# ---------------------------------------------------------------- deterministic checks


def codes(result):
    return {p["code"] for p in result["problems"]}


def test_clean_call_has_no_problems():
    turns = [
        turn("agent", "Hello, this is Asha from Acme."),
        turn("caller", "I want my order status", 3),
        turn("agent", "It ships tomorrow.", 6),
        turn("caller", "Great", 9),
        turn("agent", "Thank you, have a nice day!", 12),
    ]
    r = qa.deterministic_checks(turns)
    assert r["problems"] == []
    assert r["penalties"] == {"compliance": 0, "communication": 0}


def test_flags_greeting_silence_interruption_closing_and_claims():
    turns = [
        turn("agent", "State your issue."),
        turn("caller", "My refund is -", 5),
        turn("agent", "We will give a full refund of ₹500.", 6),
        turn("caller", "ok", 60),
        turn("agent", "Fine", 62),
    ]
    r = qa.deterministic_checks(turns)
    assert {
        "greeting_missing",
        "introduction_missing",
        "refund_claim",
        "long_silence",
        "agent_interruption",
        "closing_missing",
    } <= codes(r)
    assert r["penalties"]["compliance"] > 0 and r["penalties"]["communication"] > 0


def test_scorecard_script_and_forbidden_phrases():
    card = QAScorecard(
        name="c",
        greeting_phrases=["welcome to acme"],
        introduction_required=False,
        script_phrases=["is this call recorded"],
        forbidden_claims=["guaranteed returns"],
        closing_phrases=[],
        max_silence_seconds=5,
    )
    turns = [
        turn("agent", "Welcome to Acme."),
        turn("caller", "Hi", 1),
        turn("agent", "We offer guaranteed returns.", 12),
        turn("caller", "ok", 14),
        turn("agent", "Thanks", 15),
    ]
    r = qa.deterministic_checks(turns, card)
    c = codes(r)
    assert "forbidden_claim" in c and "script_phrase_missing" in c and "long_silence" in c
    assert "greeting_missing" not in c and "introduction_missing" not in c


def test_combine_scores_penalises_llm_and_handles_no_llm():
    rule = {"problems": [{"severity": "major", "code": "x", "message": "m", "source": "rule"}],
            "penalties": {"compliance": 30, "communication": 10}}
    llm = {"compliance": 90, "communication": 80, "resolution": 70, "sales": None, "summary": "s", "problems": []}
    r = qa.combine_scores(llm, rule)
    assert (r["compliance"], r["communication"], r["resolution"], r["sales"]) == (60, 70, 70, None)
    assert r["overall"] == round((60 + 70 + 70) / 3) and r["llm_used"]
    offline = qa.combine_scores(None, rule)
    assert offline["compliance"] == 70 and offline["resolution"] is None and not offline["llm_used"]


# ---------------------------------------------------------------- copilot parsing


def test_parse_suggestion_and_warnings():
    r = copilot.parse_suggestion(
        '{"suggested_reply": "Sure, I can help.", "intent": "Order status", "next_action": "Ask order id", '
        '"compliance_warning": null, "upsell_hint": "none", "translation_en": "Where is my order"}'
    )
    assert r["upsell_hint"] is None and r["compliance_warning"] is None and r["intent"] == "Order status"
    assert copilot.parse_suggestion('{"intent": "x"}') is None
    w = copilot.reply_compliance_warnings("We guarantee a refund", ["free gift"])
    assert len(w) == 1 and "refund" in w[0]


def test_parse_summary():
    r = copilot.parse_summary('{"summary": "S", "key_points": ["a", ""], "sentiment": "MAD"}')
    assert r == {"summary": "S", "key_points": ["a"], "action_items": [], "sentiment": "neutral"}


# ---------------------------------------------------------------- heuristics

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def pt(days_ago, sentiment=None, outcome=None, intent=None):
    return CallPoint(NOW - timedelta(days=days_ago), sentiment, outcome, intent)


def test_churn_risk_rules_are_explained():
    calls = [pt(20, "neutral"), pt(10, "negative", "complaint"), pt(5, "negative", "complaint"), pt(1, "negative", "escalated")]
    r = cx_analytics.churn_risk(calls, open_tickets=2, now=NOW)
    names = [f["factor"] for f in r["factors"]]
    assert r["level"] == "high" and r["score"] == 100 or r["score"] >= 60
    assert any("complaints in the last 90" in n for n in names)
    assert sum(f["points"] for f in r["factors"]) >= r["score"]


def test_churn_low_for_happy_customer():
    r = cx_analytics.churn_risk([pt(2, "positive", "resolved")], 0, NOW)
    assert r["score"] == 0 and r["level"] == "low"


def test_buying_intent():
    calls = [pt(30, "neutral", "sale_interest", "asks price of premium plan"), pt(2, "positive", "sale_interest")]
    r = cx_analytics.buying_intent(calls, NOW)
    assert r["level"] == "high"
    assert cx_analytics.buying_intent([pt(100, "negative")], NOW)["score"] == 0


def test_complaint_severity():
    assert cx_analytics.complaint_severity(sentiment="negative", outcome="escalated", complaints_90d=0)[0] == "high"
    assert cx_analytics.complaint_severity(sentiment="neutral", outcome="complaint", complaints_90d=1)[0] == "medium"
    assert cx_analytics.complaint_severity(sentiment="negative", outcome="resolved", complaints_90d=0)[0] == "low"


# ---------------------------------------------------------------- forecast math


def test_erlang_c_known_values():
    # A=10 Erlangs, N=12: textbook Erlang C ~ 0.4494
    assert forecast.erlang_c(10, 12) == pytest.approx(0.4494, abs=1e-3)
    assert forecast.erlang_c(10, 10) == 1.0
    assert forecast.erlang_c(0, 3) == 0.0


def test_required_agents_meets_targets():
    r = forecast.required_agents(120, 180, target_service_level=0.8, answer_seconds=20)  # A = 6
    assert r["traffic_erlangs"] == 6.0
    assert r["agents"] >= 7 and r["service_level"] >= 0.8 and r["occupancy"] <= 0.85
    fewer = forecast.service_level(6, r["agents"] - 1, 180, 20)
    assert fewer < 0.8 or 6 / (r["agents"] - 1) > 0.85
    assert forecast.required_agents(0, 180)["agents"] == 0


def test_forecast_moving_average_by_weekday_hour():
    now = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)  # Friday
    # Mondays 09:xx: 4 calls in each of 2 of the last 4 weeks -> 8/4 = 2 per Monday-9am
    times = []
    for weeks_ago in (1, 2):
        monday = datetime(2026, 9, 28, 9, 30, tzinfo=UTC) - timedelta(weeks=weeks_ago - 1)
        times += [monday - timedelta(weeks=0)] * 4
    avgs = forecast.weekday_hour_averages(times, now=now, window_weeks=4)
    assert avgs[(0, 9)] == 2.0
    assert avgs[(1, 9)] == 0.0
    days = forecast.forecast_days(times, now=now, days=3, window_weeks=4)
    assert days[0]["date"] == "2026-10-03" and days[2]["date"] == "2026-10-05"
    assert days[2]["weekday"] == "Monday" and days[2]["hourly"][9] == 2.0
    plan = forecast.staffing_plan(days, 180)
    assert plan[2]["peak_agents"] >= 1 and plan[0]["peak_agents"] == 0


def test_forecast_timezone_shift():
    now = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    t = datetime(2026, 9, 28, 20, 0, tzinfo=UTC)  # Mon 20:00 UTC = Tue 01:30 at +5:30
    avgs = forecast.weekday_hour_averages([t], now=now, window_weeks=1, tz_offset_minutes=330)
    assert avgs[(1, 1)] == 1.0


# ---------------------------------------------------------------- funnel + search helpers


def test_classify_contact_and_cost_and_rate():
    assert ops_analytics.classify_contact(None, paid_after_attempt=False)["called"] is False
    call = Call(status=CallStatus.COMPLETED, direction=CallDirection.OUTBOUND, outcome="booking_requested", duration_seconds=40)
    f = ops_analytics.classify_contact(call, paid_after_attempt=False)
    assert f == {"called": True, "connected": True, "qualified": True, "converted": True}
    ghost = Call(status=CallStatus.NO_ANSWER, direction=CallDirection.OUTBOUND)
    assert ops_analytics.classify_contact(ghost, paid_after_attempt=True)["converted"] is False
    assert ops_analytics.ai_cost_paise(150, 400) == 1000
    assert ops_analytics.rate(1, 3) == 0.3333 and ops_analytics.rate(1, 0) is None


def test_snippet_highlight_and_like_escape():
    s = transcript_search.make_snippet("I would like a REFUND for my order please", "refund", radius=8)
    assert s == {"before": "… like a ", "match": "REFUND", "after": " for my …"}
    assert transcript_search.make_snippet("hello", "xyz") is None
    assert transcript_search.escape_like("50%_off") == r"50\%\_off"


# ---------------------------------------------------------------- API


async def _signup(client, slug):
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"company_name": slug, "tenant_slug": slug, "full_name": "O", "email": f"o@{slug}.com", "password": "supersecret1"},
    )
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _seed_call(db, tenant_id, texts, **call_fields):
    from packages.database.models import (
        Conversation,
        ConversationChannel,
        Message,
        MessageDirection,
        MessageSender,
    )

    call_fields.setdefault("direction", CallDirection.INBOUND)
    call = Call(tenant_id=tenant_id, status=CallStatus.COMPLETED, from_number="+911", **call_fields)
    db.add(call)
    await db.flush()
    conv = Conversation(tenant_id=tenant_id, channel=ConversationChannel.VOICE, call_id=call.id)
    db.add(conv)
    await db.flush()
    for who, text in texts:
        db.add(
            Message(
                tenant_id=tenant_id, conversation_id=conv.id,
                direction=MessageDirection.INBOUND if who == "c" else MessageDirection.OUTBOUND,
                sender=MessageSender.CUSTOMER if who == "c" else MessageSender.AI_AGENT, text=text,
            )
        )
    await db.commit()
    return call


async def test_api_search_scorecards_scoring_and_analytics(client, db_session, monkeypatch):
    import uuid

    from packages.database.models import Tenant

    headers = await _signup(client, "insights-co")
    tenant = (await db_session.execute(__import__("sqlalchemy").select(Tenant).where(Tenant.slug == "insights-co"))).scalar_one()
    other_headers = await _signup(client, "insights-other")

    call = await _seed_call(
        db_session, tenant.id,
        [("a", "Hello, this is Asha."), ("c", "I need a refund for 100% of my order"), ("a", "Sure, thanks. Bye"), ("c", "ok")],
        sentiment="negative", outcome="complaint", intent="Refund request", duration_seconds=90, analyzed_at=datetime.now(UTC),
    )

    # search: ILIKE, highlighted, tenant isolated
    res = await client.get("/api/v1/insights/search", params={"q": "REFUND"}, headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body) == 1 and body[0]["call_id"] == str(call.id)
    assert body[0]["snippet"]["match"].lower() == "refund" and body[0]["speaker"] == "caller"
    assert (await client.get("/api/v1/insights/search", params={"q": "refund"}, headers=other_headers)).json() == []
    assert (await client.get("/api/v1/insights/search", params={"q": "100%"}, headers=headers)).json()[0]["match_count"] == 1

    # scorecards CRUD and single default
    c1 = await client.post("/api/v1/insights/qa/scorecards", headers=headers, json={"name": "A", "is_default": True, "forbidden_claims": [" free gift ", ""]})
    assert c1.status_code == 201 and c1.json()["forbidden_claims"] == ["free gift"]
    c2 = await client.post("/api/v1/insights/qa/scorecards", headers=headers, json={"name": "B", "is_default": True})
    cards = (await client.get("/api/v1/insights/qa/scorecards", headers=headers)).json()
    assert {c["name"]: c["is_default"] for c in cards} == {"A": False, "B": True}
    assert (await client.patch(f"/api/v1/insights/qa/scorecards/{c1.json()['id']}", headers=headers, json={"script_phrases": ["x"]})).json()["script_phrases"] == ["x"]
    assert (await client.delete(f"/api/v1/insights/qa/scorecards/{c2.json()['id']}", headers=headers)).status_code == 204
    assert (await client.get("/api/v1/insights/qa/scorecards", headers=other_headers)).json() == []

    # rescore with no LLM key: rule-based result is still stored
    resp = await client.post(f"/api/v1/insights/qa/scores/{call.id}/rescore", headers=headers, json={})
    assert resp.status_code == 200, resp.text
    score = resp.json()
    assert score["llm_used"] is False and score["resolution"] is None
    assert any(p["code"] == "refund_claim" for p in score["problems"]) is False  # claim rules look at agent turns only
    listing = (await client.get("/api/v1/insights/qa/scores", headers=headers)).json()
    assert len(listing) == 1 and listing[0]["call_id"] == str(call.id)
    assert (await client.get(f"/api/v1/insights/qa/scores/{call.id}", headers=headers)).status_code == 200
    assert (await client.get(f"/api/v1/insights/qa/scores/{call.id}", headers=other_headers)).status_code == 404
    assert (await client.get("/api/v1/insights/qa/summary", headers=headers)).json()["scored_calls"] == 1
    assert (await client.post(f"/api/v1/insights/qa/scores/{uuid.uuid4()}/rescore", headers=headers, json={})).status_code == 404

    # analytics endpoints respond with data
    assert (await client.get("/api/v1/insights/cx/sentiment-trend", headers=headers)).json()[0]["negative"] == 1
    assert (await client.get("/api/v1/insights/cx/intents", headers=headers)).json() == [{"intent": "refund request", "count": 1}]
    comp = (await client.get("/api/v1/insights/cx/complaints", headers=headers)).json()
    assert comp["counts"]["medium"] == 1
    assert (await client.get("/api/v1/insights/cx/customer-scores", headers=headers)).json() == []  # call has no customer
    assert (await client.get("/api/v1/insights/cx/repeat-callers", headers=headers)).json() == []
    assert (await client.get("/api/v1/insights/cx/customer-value", headers=headers)).json() == []
    ai = (await client.get("/api/v1/insights/analytics/ai", headers=headers)).json()
    assert ai["ai_calls"] == 0
    assert (await client.get("/api/v1/insights/analytics/agents", headers=headers)).json() == []
    assert (await client.get("/api/v1/insights/analytics/campaigns", headers=headers)).json() == []
    fc = (await client.get("/api/v1/insights/analytics/forecast", headers=headers)).json()
    assert fc["aht_seconds"] == 90 and len(fc["forecast"]) == 7 and fc["staffing"] is not None

    # copilot without any Sarvam key fails clearly, not with fake output
    sug = await client.post("/api/v1/insights/copilot/suggest", headers=headers, json={"caller_text": "hi", "call_id": str(call.id)})
    assert sug.status_code == 502 and "API key" in sug.json()["detail"]


async def test_customer_and_ops_queries_with_data(client, db_session):
    from sqlalchemy import select

    from packages.database.models import (
        AIAgent, Campaign, CampaignChannel, CampaignContact, CampaignContactStatus, Customer,
        Payment, PaymentLink, PaymentStatus, Tenant, Ticket,
    )

    headers = await _signup(client, "insights-data")
    tenant = (await db_session.execute(select(Tenant).where(Tenant.slug == "insights-data"))).scalar_one()
    customer = Customer(tenant_id=tenant.id, name="Ravi", phone="+9199")
    agent = AIAgent(tenant_id=tenant.id, name="Bot")
    db_session.add_all([customer, agent])
    await db_session.flush()
    now = datetime.now(UTC)
    for direction in (CallDirection.INBOUND, CallDirection.OUTBOUND):
        await _seed_call(
            db_session, tenant.id, [("a", "Hi"), ("c", "price of plan?")], direction=direction,
            customer_id=customer.id, ai_agent_id=agent.id, sentiment="negative", outcome="complaint",
            intent="Plan price", duration_seconds=60, analyzed_at=now,
        )
    db_session.add(Ticket(tenant_id=tenant.id, customer_id=customer.id, subject="Broken"))
    link = PaymentLink(tenant_id=tenant.id, customer_id=customer.id, provider_payment_link_id="pl1", amount_paise=5000, short_url="u")
    db_session.add(link)
    await db_session.flush()
    db_session.add(Payment(tenant_id=tenant.id, payment_link_id=link.id, provider_payment_id="p1", amount_paise=5000, status=PaymentStatus.CAPTURED))
    campaign = Campaign(tenant_id=tenant.id, name="Promo", channel=CampaignChannel.VOICE, ai_agent_id=agent.id)
    db_session.add(campaign)
    await db_session.flush()
    db_session.add(CampaignContact(tenant_id=tenant.id, campaign_id=campaign.id, customer_id=customer.id,
                                   status=CampaignContactStatus.COMPLETED, attempted_at=now - timedelta(hours=1)))
    await db_session.commit()

    scores = (await client.get("/api/v1/insights/cx/customer-scores", headers=headers)).json()
    assert scores[0]["name"] == "Ravi" and scores[0]["churn_risk"]["score"] > 0
    cid = scores[0]["customer_id"]
    assert (await client.get("/api/v1/insights/cx/customer-scores", params={"sort": "buying"}, headers=headers)).status_code == 200
    one = (await client.get(f"/api/v1/insights/cx/customers/{cid}/scores", headers=headers)).json()
    assert one["calls"] == 2
    journey = (await client.get(f"/api/v1/insights/cx/customers/{cid}/journey", headers=headers)).json()
    assert {e["type"] for e in journey} == {"call", "ticket", "payment"}
    value = (await client.get("/api/v1/insights/cx/customer-value", headers=headers)).json()
    assert value[0]["net_paise"] == 5000
    repeat = (await client.get("/api/v1/insights/cx/repeat-callers", headers=headers)).json()
    assert len(repeat) == 0  # only one inbound call from this number
    agents = (await client.get("/api/v1/insights/analytics/agents", headers=headers)).json()
    assert agents[0]["calls"] == 2 and agents[0]["aht_seconds"] == 60 and agents[0]["resolution_rate"] == 0
    ai = (await client.get("/api/v1/insights/analytics/ai", headers=headers)).json()
    assert ai["ai_calls"] == 2 and ai["total_cost_paise"] == 800 and ai["cost_per_call_paise"] == 400
    camp = (await client.get("/api/v1/insights/analytics/campaigns", headers=headers)).json()
    assert (camp[0]["calls"], camp[0]["connected"], camp[0]["qualified"], camp[0]["converted"]) == (1, 1, 0, 1)


@pytest.mark.asyncio
async def test_human_agent_analytics_groups_by_handler(db_session) -> None:
    import uuid

    from app.services import ops_analytics
    from packages.database.models import Call, CallDirection, CallStatus, Customer, Tenant, User
    from packages.database.models.workspace import CallHandoff, HandlerState

    tenant = Tenant(name="T", slug=f"t-{uuid.uuid4().hex[:8]}")
    db_session.add(tenant)
    await db_session.flush()
    user = User(email=f"{uuid.uuid4().hex[:8]}@x.io", hashed_password="x", full_name="Asha Rao")
    db_session.add(user)
    await db_session.flush()
    for outcome, sentiment in (("resolved", "positive"), ("complaint", "negative")):
        call = Call(
            tenant_id=tenant.id, direction=CallDirection.INBOUND, status=CallStatus.COMPLETED,
            duration_seconds=120, outcome=outcome, sentiment=sentiment,
        )
        db_session.add(call)
        await db_session.flush()
        db_session.add(
            CallHandoff(
                tenant_id=tenant.id, call_id=call.id, state=HandlerState.HUMAN,
                handler_user_id=user.id, taken_at=datetime.now(UTC),
            )
        )
    await db_session.flush()
    rows = await ops_analytics.human_agent_analytics(db_session, tenant.id, 30)
    assert len(rows) == 1
    r = rows[0]
    assert r["name"] == "Asha Rao" and r["calls"] == 2 and r["aht_seconds"] == 120
    assert r["resolution_rate"] == 0.5 and r["positive_calls"] == 1 and r["negative_calls"] == 1
