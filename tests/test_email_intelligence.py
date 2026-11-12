"""Open and click tracking, the send-time engine, the email checker, AI subjects, auto-schedule and autopilot."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.core.config import get_settings
from app.services import email_ai, email_autopilot, email_checker, email_insights, send_time
from app.services import email_tracking as tracking
from app.worker import campaign_tasks
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Campaign,
    CampaignChannel,
    CampaignContact,
    CampaignContactStatus,
    CampaignStatus,
    Customer,
    PrivacyProfile,
    Tenant,
)
from packages.database.models.email_event import EmailEvent

ADDRESS = "12 Park Road, Pune 411001"
GIF = b"GIF89a"


@pytest.fixture(autouse=True)
def _keep_rows_loaded(request: pytest.FixtureRequest) -> None:
    """Rows stay readable after a commit, so a test can use a customer's id after the route committed."""
    if "db_session" in request.fixturenames:
        request.getfixturevalue("db_session").sync_session.expire_on_commit = False


async def _owner(client: AsyncClient, slug: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"company_name": slug.title(), "tenant_slug": slug, "full_name": "Owner",
              "email": f"owner@{slug}.com", "password": "supersecret1"},
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _tenant_of(db: AsyncSession, slug: str) -> Tenant:
    return (await db.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one()


async def _setup(db: AsyncSession, tenant: Tenant, emails: list[str], *, status=CampaignStatus.RUNNING) -> tuple[Campaign, list[Customer]]:
    db.add(PrivacyProfile(tenant_id=tenant.id, legal_name="Co Pvt Ltd", address=ADDRESS))
    campaign = Campaign(
        tenant_id=tenant.id, name="Promo", channel=CampaignChannel.EMAIL, status=status,
        email_subject="Hello {{first_name}}", email_body="See https://shop.example.com/sale?a=1&b=2 today.",
    )
    db.add(campaign)
    await db.flush()
    customers = []
    for email in emails:
        customer = Customer(tenant_id=tenant.id, name=email.split("@")[0].title(), email=email)
        db.add(customer)
        await db.flush()
        customers.append(customer)
        db.add(CampaignContact(tenant_id=tenant.id, campaign_id=campaign.id, customer_id=customer.id, status=CampaignContactStatus.SENT))
    await db.commit()
    return campaign, customers


# ---- signed tracking tokens ---------------------------------------------------------------------


def test_tracking_tokens_round_trip_and_reject_tampering() -> None:
    t, c, p = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert tracking.parse_token(tracking.make_token(t, c, p)) == (t, c, p, None)
    link = tracking.make_token(t, c, p, "https://shop.example.com/a?x=1")
    assert tracking.parse_token(link) == (t, c, p, "https://shop.example.com/a?x=1")
    # Swapping the destination breaks the signature, so the redirect cannot be pointed elsewhere.
    forged = link.rsplit(".", 2)[0] + "." + tracking._b64(b"https://evil.example.com") + "." + link.rsplit(".", 1)[1]
    assert tracking.parse_token(forged) is None
    assert tracking.parse_token(link + "x") is None
    assert tracking.parse_token("garbage") is None
    # Even a correctly signed token is refused if it is not a web address.
    body = ".".join([t.hex, c.hex, p.hex, tracking._b64(b"javascript:alert(1)")])
    assert tracking.parse_token(f"{body}.{tracking._sign(body)}") is None


def test_link_and_track_wraps_web_addresses_and_adds_one_pixel() -> None:
    t, c, p = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    html = tracking.link_and_track("Visit https://shop.example.com/sale?a=1&amp;b=2. Thanks", t, c, p)
    assert html.count("<img") == 1 and "/email/t/o/" in html
    assert 'href="http://localhost:8000/api/v1/email/t/c/' in html
    token = html.split("/email/t/c/")[1].split('"')[0]
    assert tracking.parse_token(token)[3] == "https://shop.example.com/sale?a=1&b=2"  # & restored, trailing "." kept out
    assert ">https://shop.example.com/sale?a=1&amp;b=2</a>. Thanks" in html


def test_tracking_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "email_tracking_enabled", False)
    html = tracking.link_and_track("Go to https://a.example.com now", uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    assert "<img" not in html and 'href="https://a.example.com"' in html


# ---- the public pixel and link ------------------------------------------------------------------


async def test_pixel_and_link_record_events_and_redirect_only_to_the_signed_address(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _owner(client, "track-co")
    tenant = await _tenant_of(db_session, "track-co")
    campaign, (priya,) = await _setup(db_session, tenant, ["priya@example.com"])

    pixel = await client.get(f"/api/v1/email/t/o/{tracking.make_token(tenant.id, campaign.id, priya.id)}.gif", headers={"user-agent": "Mozilla/5.0"})
    assert pixel.status_code == 200 and pixel.content.startswith(GIF) and "no-store" in pixel.headers["cache-control"]

    url = "https://shop.example.com/sale?a=1"
    link = await client.get(f"/api/v1/email/t/c/{tracking.make_token(tenant.id, campaign.id, priya.id, url)}", headers={"user-agent": "Mozilla/5.0"}, follow_redirects=False)
    assert link.status_code == 302 and link.headers["location"] == url

    kinds = sorted(e.kind for e in (await db_session.execute(select(EmailEvent))).scalars().all())
    assert kinds == ["click", "open"]
    stats = await email_insights.engagement_by_campaign(db_session, tenant.id, [campaign.id])
    assert stats[campaign.id] == {"opened": 1, "clicked": 1, "clicks": 1}


async def test_bad_tokens_record_nothing_and_scanners_are_ignored(client: AsyncClient, db_session: AsyncSession) -> None:
    await _owner(client, "scan-co")
    tenant = await _tenant_of(db_session, "scan-co")
    campaign, (priya,) = await _setup(db_session, tenant, ["priya@example.com"])
    bad = await client.get("/api/v1/email/t/o/not-a-token.gif")
    assert bad.status_code == 200 and bad.content.startswith(GIF)  # always an image, never an error page
    assert (await client.get("/api/v1/email/t/c/not-a-token")).status_code == 404
    # A link token without a destination cannot be used as a redirect.
    assert (await client.get(f"/api/v1/email/t/c/{tracking.make_token(tenant.id, campaign.id, priya.id)}")).status_code == 404
    await client.get(f"/api/v1/email/t/o/{tracking.make_token(tenant.id, campaign.id, priya.id)}.gif", headers={"user-agent": "SecurityScanner-bot/1.0"})
    db_session.expire_all()
    assert (await db_session.execute(select(EmailEvent))).scalars().all() == []


async def test_a_deleted_campaign_still_returns_the_image(client: AsyncClient, db_session: AsyncSession) -> None:
    await _owner(client, "gone-co")
    tenant = await _tenant_of(db_session, "gone-co")
    token = tracking.make_token(tenant.id, uuid.uuid4(), uuid.uuid4())  # campaign never existed
    response = await client.get(f"/api/v1/email/t/o/{token}.gif", headers={"user-agent": "Mozilla/5.0"})
    assert response.status_code == 200 and response.content.startswith(GIF)


async def test_the_campaign_email_carries_tracked_links_but_the_unsubscribe_link_is_left_alone(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _owner(client, "wrap-co")
    tenant = await _tenant_of(db_session, "wrap-co")
    campaign, (priya,) = await _setup(db_session, tenant, ["priya@example.com"])
    captured: dict = {}

    async def fake_send(*, to_email, subject, html_body, headers=None, tenant_id=None, credentials=None):
        captured["html"] = html_body

    async def fake_reserve(db, tid):
        return object()

    async def fake_sync(db, tid):
        return 0

    monkeypatch.setattr(get_settings(), "email_api_key", "k")
    monkeypatch.setattr(campaign_tasks, "send_email", fake_send)
    monkeypatch.setattr(campaign_tasks.plans, "reserve_email", fake_reserve)
    monkeypatch.setattr(campaign_tasks.plans, "sync_overage_invoice", fake_sync)
    await campaign_tasks._send_campaign_email(db_session, campaign, priya)
    html = captured["html"]
    assert "/email/t/c/" in html and html.count("<img") == 1
    assert "/email/unsubscribe/" in html and "/email/t/c/" not in html.split("/email/unsubscribe/")[1].split('"')[0]


# ---- send-time engine ---------------------------------------------------------------------------


def test_with_no_data_the_default_is_midweek_mid_morning() -> None:
    now = datetime(2026, 11, 9, 6, 0, tzinfo=UTC)  # a Monday
    slots = send_time.best_slots(send_time.empty_grid(), "UTC", now)
    assert len(slots) == 3 and slots[0]["source"] == "default"
    assert slots[0]["local"].endswith("10:00") and slots[0]["local"].split()[0] in ("Tuesday", "Wednesday", "Thursday")
    assert slots[0]["score"] == 100 and len({s["at"][:10] for s in slots}) == 3  # three different days
    assert all(datetime.fromisoformat(s["at"]) > now + timedelta(minutes=30) for s in slots)


def test_slots_are_in_the_business_time_zone_and_never_in_the_night() -> None:
    now = datetime(2026, 11, 9, 0, 0, tzinfo=UTC)
    for slot in send_time.best_slots(send_time.empty_grid(), "Asia/Kolkata", now, count=6):
        local = datetime.fromisoformat(slot["at"]).astimezone(send_time.zone("Asia/Kolkata"))
        assert 8 <= local.hour <= 20
    # 10:00 in Kolkata is 04:30 UTC.
    first = datetime.fromisoformat(send_time.best_slots(send_time.empty_grid(), "Asia/Kolkata", now)[0]["at"])
    assert (first.hour, first.minute) == (4, 30)


def test_the_workspaces_own_opens_take_over_from_the_default() -> None:
    tz = send_time.zone("UTC")
    grid = send_time.empty_grid()
    for _ in range(80):  # every reader opens on Saturday evenings
        send_time.add_event(grid, datetime(2026, 11, 7, 19, 15, tzinfo=UTC), tz)
    slots = send_time.best_slots(grid, "UTC", datetime(2026, 11, 9, 6, 0, tzinfo=UTC), count=1)
    assert slots[0]["local"] == "Saturday 19:00" and slots[0]["source"] == "learned"
    assert "Your readers open most on Saturdays" in slots[0]["reason"]


def test_a_little_data_only_nudges_the_default() -> None:
    tz = send_time.zone("UTC")
    grid = send_time.empty_grid()
    for _ in range(5):
        send_time.add_event(grid, datetime(2026, 11, 7, 19, 0, tzinfo=UTC), tz)
    _, learned = send_time.blended(grid)
    assert 0 < learned < 0.2
    assert send_time.best_slots(grid, "UTC", datetime(2026, 11, 9, 6, 0, tzinfo=UTC), count=1)[0]["source"] == "mixed"


def test_an_unknown_time_zone_falls_back_to_utc() -> None:
    assert str(send_time.zone("Mars/Olympus")) == "UTC"


# ---- the checker --------------------------------------------------------------------------------


def test_a_clean_personal_email_scores_well() -> None:
    result = email_checker.check_email(
        "Your service is due next week",
        "Hi {{first_name}}, your car service is due next week. Reply to this email and we will find a time that suits you. "
        "Our workshop is open from nine to six on weekdays and we are happy to help with anything you need.",
    )
    assert result["score"] >= 90 and result["label"] == "good" and result["issues"] == []


def test_a_spammy_email_is_flagged_with_reasons() -> None:
    result = email_checker.check_email(
        "FREE WINNER!!! ACT NOW",
        "CLICK HERE to claim your FREE cash prize! Limited time, guaranteed! Visit https://bit.ly/abc {{coupon}}",
    )
    assert result["label"] == "risky" and result["score"] < 60
    text = " ".join(i["text"] for i in result["issues"])
    assert "capitals" in text and "shortened link" in text and "{{coupon}}" in text and "Spam-filter words" in text


def test_missing_subject_and_body_are_the_worst() -> None:
    result = email_checker.check_email("", "")
    assert result["label"] == "risky" and {i["field"] for i in result["issues"]} == {"subject", "body"}


def test_fake_reply_subjects_and_long_bodies_are_caught() -> None:
    result = email_checker.check_email("Re: your order", "word " * 320)
    levels = {i["text"][:20] for i in result["issues"]}
    assert any("pretends it is a reply" in i["text"] for i in result["issues"]) and any("320 words" in i["text"] for i in result["issues"]), levels


# ---- AI subject lines ---------------------------------------------------------------------------


def test_subject_variants_are_parsed_deduplicated_and_cleaned() -> None:
    text = '<think>hmm</think>{"variants": [{"subject": "Your slot, {{first_name}}", "angle": "Personal"}, {"subject": "your slot, {{first_name}}", "angle": "Dup"}, {"subject": "", "angle": "Empty"}, "junk", {"subject": "Ready to book?"}]}'
    assert email_ai.parse_subjects(text) == [
        {"subject": "Your slot, {{first_name}}", "angle": "Personal"},
        {"subject": "Ready to book?", "angle": "Variant"},
    ]
    assert email_ai.parse_subjects("not json") == [] and email_ai.parse_subjects('{"variants": 3}') == []


async def test_subjects_route_scores_and_ranks_the_ai_variants(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    headers = await _owner(client, "subj-co")

    async def fake_suggest(*, subject, body, api_key):
        return [{"subject": "WIN FREE CASH NOW!!", "angle": "Loud"}, {"subject": "Your service is due, {{first_name}}", "angle": "Personal"}]

    from app.api.v1 import email_intelligence

    monkeypatch.setattr(email_intelligence.email_ai, "suggest_subjects", fake_suggest)
    body = "Hi {{first_name}}, your car service is due next week. Reply and we will find a time that suits you, whatever day works best for you."
    ok = await client.post("/api/v1/email/ai/subjects", headers=headers, json={"subject": "Service", "body": body})
    assert ok.status_code == 200
    variants = ok.json()["variants"]
    assert [v["angle"] for v in variants] == ["Personal", "Loud"] and variants[0]["score"] > variants[1]["score"]
    empty = await client.post("/api/v1/email/ai/subjects", headers=headers, json={"subject": "x", "body": ""})
    assert empty.status_code == 422


async def test_check_route_needs_no_ai(client: AsyncClient) -> None:
    headers = await _owner(client, "chk-co")
    response = await client.post("/api/v1/email/ai/check", headers=headers, json={"subject": "Hello", "body": "Short."})
    assert response.status_code == 200 and response.json()["stats"]["words"] == 1


# ---- send times, auto-schedule and the report ---------------------------------------------------


async def test_send_times_route_learns_from_events_without_being_ruled_by_one_burst(client: AsyncClient, db_session: AsyncSession) -> None:
    headers = await _owner(client, "time-co")
    tenant = await _tenant_of(db_session, "time-co")
    campaign, customers = await _setup(db_session, tenant, ["a@example.com"])
    fresh = await client.get("/api/v1/email/ai/send-times", headers=headers)
    assert fresh.status_code == 200 and fresh.json()["events"] == 0 and fresh.json()["slots"][0]["source"] == "default"
    assert len(fresh.json()["heatmap"]) == 7 and len(fresh.json()["heatmap"][0]) == 24
    when = datetime.now(UTC) - timedelta(days=3)
    for _ in range(64):  # one campaign, everyone opening in the hour after it went out
        db_session.add(EmailEvent(tenant_id=tenant.id, campaign_id=campaign.id, customer_id=customers[0].id, kind="open", created_at=when.replace(hour=18, minute=5)))
    await db_session.commit()
    body = (await client.get("/api/v1/email/ai/send-times", headers=headers)).json()
    assert body["events"] == 64
    cell = body["heatmap"][when.replace(hour=18).weekday()][18]
    assert cell == 8  # 64 opens in one hour of one campaign count as sqrt(64)
    assert 0 < body["learned"] < 0.2 and body["slots"][0]["source"] == "mixed"
    # Opens at that hour across several campaigns do add up.
    for index in range(8):
        other = Campaign(tenant_id=tenant.id, name=f"C{index}", channel=CampaignChannel.EMAIL, status=CampaignStatus.COMPLETED, email_subject="s", email_body="b")
        db_session.add(other)
        await db_session.flush()
        for _ in range(64):
            db_session.add(EmailEvent(tenant_id=tenant.id, campaign_id=other.id, customer_id=customers[0].id, kind="open", created_at=when.replace(hour=18, minute=20)))
    await db_session.commit()
    learned = (await client.get("/api/v1/email/ai/send-times", headers=headers)).json()
    assert learned["learned"] == 1.0 and learned["slots"][0]["source"] == "learned" and learned["slots"][0]["local"].endswith("18:00")


async def test_auto_schedule_picks_the_best_time_and_can_launch(client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "email_api_key", "k")
    headers = await _owner(client, "auto-co")
    tenant = await _tenant_of(db_session, "auto-co")
    campaign, _ = await _setup(db_session, tenant, ["a@example.com"], status=CampaignStatus.DRAFT)
    db_session.add(CampaignContact(tenant_id=tenant.id, campaign_id=campaign.id, customer_id=(await db_session.execute(select(Customer.id))).scalars().first()))
    plan = (await client.get("/api/v1/email/ai/send-times", headers=headers)).json()

    bad = await client.post(f"/api/v1/campaigns/{campaign.id}/auto-schedule", headers=headers, json={"at": "2030-01-01T00:00:00Z"})
    assert bad.status_code == 422  # not one of the suggested times

    chosen = plan["slots"][1]
    scheduled = await client.post(f"/api/v1/campaigns/{campaign.id}/auto-schedule", headers=headers, json={"at": chosen["at"]})
    assert scheduled.status_code == 200 and scheduled.json()["scheduled_at"] == chosen["at"] and scheduled.json()["status"] == "draft"

    launched = await client.post(f"/api/v1/campaigns/{campaign.id}/auto-schedule", headers=headers, json={"launch": True})
    assert launched.status_code == 200, launched.text
    assert launched.json()["status"] == "scheduled" and launched.json()["scheduled_at"] == plan["slots"][0]["at"]
    await db_session.refresh(campaign)
    assert campaign.status == CampaignStatus.SCHEDULED
    again = await client.post(f"/api/v1/campaigns/{campaign.id}/auto-schedule", headers=headers, json={"launch": True})
    assert again.status_code == 200  # still scheduled: it can be moved to another time


async def test_auto_schedule_refuses_other_channels_and_other_workspaces(client: AsyncClient, db_session: AsyncSession) -> None:
    headers = await _owner(client, "chan-co")
    other = await _owner(client, "other-co")
    tenant = await _tenant_of(db_session, "chan-co")
    sms = Campaign(tenant_id=tenant.id, name="S", channel=CampaignChannel.SMS, status=CampaignStatus.DRAFT, sms_body="hi")
    db_session.add(sms)
    await db_session.commit()
    assert (await client.post(f"/api/v1/campaigns/{sms.id}/auto-schedule", headers=headers, json={})).status_code == 422
    assert (await client.post(f"/api/v1/campaigns/{sms.id}/auto-schedule", headers=other, json={})).status_code == 404


async def test_report_and_overview_show_opens_and_clicks(client: AsyncClient, db_session: AsyncSession) -> None:
    headers = await _owner(client, "rep-co")
    tenant = await _tenant_of(db_session, "rep-co")
    campaign, customers = await _setup(db_session, tenant, ["a@example.com", "b@example.com", "c@example.com", "d@example.com"])
    for c in customers[:3]:
        db_session.add(EmailEvent(tenant_id=tenant.id, campaign_id=campaign.id, customer_id=c.id, kind="open"))
    db_session.add(EmailEvent(tenant_id=tenant.id, campaign_id=campaign.id, customer_id=customers[0].id, kind="open"))  # same person again
    for _ in range(2):
        db_session.add(EmailEvent(tenant_id=tenant.id, campaign_id=campaign.id, customer_id=customers[0].id, kind="click", url="https://shop.example.com/sale"))
    await db_session.commit()

    report = (await client.get(f"/api/v1/email/campaigns/{campaign.id}/report", headers=headers)).json()
    assert (report["sent"], report["opened"], report["clicked"], report["open_rate"], report["click_rate"]) == (4, 3, 1, 75.0, 25.0)
    assert report["links"] == [{"url": "https://shop.example.com/sale", "clicks": 2, "people": 1}]
    overview = (await client.get("/api/v1/email/overview", headers=headers)).json()
    assert overview["opens"] == 3 and overview["clicks"] == 1
    assert (overview["campaigns"][0]["opened"], overview["campaigns"][0]["clicked"]) == (3, 1)
    other = await _owner(client, "peek-co")
    assert (await client.get(f"/api/v1/email/campaigns/{campaign.id}/report", headers=other)).status_code == 404


# ---- weekly autopilot ---------------------------------------------------------------------------


@pytest.fixture
def fake_draft(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    async def draft(*, instruction, current_subject, current_body, api_key):
        calls.append(instruction)
        return {"subject": "A tip for you, {{first_name}}", "body": "Hi {{first_name}}, here is this week's tip from us. Reply if you would like help putting it to use in your own home or shop."}

    monkeypatch.setattr(email_autopilot.email_ai, "draft_email", draft)
    monkeypatch.setattr(get_settings(), "email_api_key", "k")
    return calls


async def _autopilot_tenant(client: AsyncClient, db: AsyncSession, slug: str, *, mode: str = "review", day: int = 0) -> tuple[dict, Tenant]:
    headers = await _owner(client, slug)
    tenant = await _tenant_of(db, slug)
    db.add(PrivacyProfile(tenant_id=tenant.id, legal_name="Co", address=ADDRESS))
    db.add(Customer(tenant_id=tenant.id, name="Priya Shah", email="priya@example.com"))
    db.add(Customer(tenant_id=tenant.id, name="No Mail", email=None))
    await db.commit()
    saved = await client.put("/api/v1/email/autopilot", headers=headers, json={"enabled": True, "goal": "A weekly tip", "mode": mode, "day": day})
    assert saved.status_code == 200, saved.text
    await db.refresh(tenant)  # the route saved through its own session
    return headers, tenant


async def test_autopilot_needs_a_goal_to_turn_on(client: AsyncClient) -> None:
    headers = await _owner(client, "goal-co")
    assert (await client.put("/api/v1/email/autopilot", headers=headers, json={"enabled": True, "goal": " "})).status_code == 422
    off = await client.put("/api/v1/email/autopilot", headers=headers, json={"enabled": False})
    assert off.status_code == 200 and off.json()["enabled"] is False


async def test_autopilot_drafts_a_campaign_for_review_once_a_week(client: AsyncClient, db_session: AsyncSession, fake_draft) -> None:
    _, tenant = await _autopilot_tenant(client, db_session, "pilot-co", day=0)
    monday = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)  # Monday 09:00 UTC
    result = await email_autopilot.run_for_tenant(db_session, tenant, monday)
    assert result["status"] == "drafted" and result["score"] >= 70
    campaign = await db_session.get(Campaign, uuid.UUID(result["campaign_id"]))
    assert campaign.status == CampaignStatus.DRAFT and campaign.scheduled_at > monday and campaign.email_subject.startswith("A tip")
    contacts = (await db_session.execute(select(CampaignContact).where(CampaignContact.campaign_id == campaign.id))).scalars().all()
    assert len(contacts) == 1  # only the person with an email address
    # Same week: nothing more. Wrong weekday: nothing. Next week: another.
    assert (await email_autopilot.run_for_tenant(db_session, tenant, monday + timedelta(hours=3)))["status"] == "skipped"
    assert (await email_autopilot.run_for_tenant(db_session, tenant, monday + timedelta(days=9)))["reason"] == "Not due yet."
    assert (await email_autopilot.run_for_tenant(db_session, tenant, monday + timedelta(days=7)))["status"] == "drafted"
    assert len(fake_draft) == 2


async def test_autopilot_send_mode_schedules_only_a_strong_draft(client: AsyncClient, db_session: AsyncSession, fake_draft, monkeypatch: pytest.MonkeyPatch) -> None:
    _, tenant = await _autopilot_tenant(client, db_session, "send-co", mode="send")
    monday = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)
    first = await email_autopilot.run_for_tenant(db_session, tenant, monday)
    assert first["status"] == "scheduled"
    assert (await db_session.get(Campaign, uuid.UUID(first["campaign_id"]))).status == CampaignStatus.SCHEDULED

    async def spammy(*, instruction, current_subject, current_body, api_key):
        return {"subject": "FREE WINNER ACT NOW!!!", "body": "CLICK HERE for FREE CASH, guaranteed! https://bit.ly/x"}

    monkeypatch.setattr(email_autopilot.email_ai, "draft_email", spammy)
    second = await email_autopilot.run_for_tenant(db_session, tenant, monday + timedelta(days=7))
    assert second["status"] == "drafted"  # too risky to send unattended
    assert (await db_session.get(Campaign, uuid.UUID(second["campaign_id"]))).status == CampaignStatus.DRAFT


async def test_autopilot_does_not_mark_the_week_when_it_could_not_run(client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    headers = await _owner(client, "block-co")
    tenant = await _tenant_of(db_session, "block-co")
    db_session.add(Customer(tenant_id=tenant.id, name="Priya", email="priya@example.com"))
    await db_session.commit()
    await client.put("/api/v1/email/autopilot", headers=headers, json={"enabled": True, "goal": "A tip", "day": 0})
    await db_session.refresh(tenant)
    monkeypatch.setattr(get_settings(), "email_api_key", "k")
    monday = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)
    no_address = await email_autopilot.run_for_tenant(db_session, tenant, monday)
    assert no_address["status"] == "skipped" and "postal address" in no_address["reason"]
    assert (await db_session.get(Tenant, tenant.id)).email_autopilot.get("last_week") is None  # it will try again


async def test_autopilot_never_includes_suppressed_people(client: AsyncClient, db_session: AsyncSession, fake_draft) -> None:
    from app.services import email_compliance

    _, tenant = await _autopilot_tenant(client, db_session, "supp-pilot-co")
    await email_compliance.suppress(db_session, tenant.id, "priya@example.com", reason="unsubscribed", source="unsubscribe_link")
    await db_session.commit()
    result = await email_autopilot.run_for_tenant(db_session, tenant, datetime(2026, 11, 9, 9, 0, tzinfo=UTC))
    assert result["status"] == "skipped" and "No one can be emailed" in result["reason"] and fake_draft == []


async def test_run_now_always_leaves_a_draft_even_in_send_mode(client: AsyncClient, db_session: AsyncSession, fake_draft) -> None:
    headers, tenant = await _autopilot_tenant(client, db_session, "now-co", mode="send")
    ran = await client.post("/api/v1/email/autopilot/run", headers=headers)
    assert ran.status_code == 200 and ran.json()["status"] == "drafted", ran.text
    db_session.expire_all()
    assert (await db_session.get(Campaign, uuid.UUID(ran.json()["campaign_id"]))).status == CampaignStatus.DRAFT
    shown = (await client.get("/api/v1/email/autopilot", headers=headers)).json()
    assert shown["mode"] == "send" and shown["last_campaign_id"] == ran.json()["campaign_id"] and shown["ready"] is None


async def test_the_scheduler_sweep_only_drafts_for_workspaces_that_are_due(client: AsyncClient, db_session: AsyncSession, fake_draft) -> None:
    from contextlib import asynccontextmanager

    from app.worker.celery_app import celery_app

    @asynccontextmanager
    async def factory():
        yield db_session

    _, due = await _autopilot_tenant(client, db_session, "sweep-due-co", day=0)
    off_headers = await _owner(client, "sweep-off-co")  # never turned on
    other = await _tenant_of(db_session, "sweep-off-co")
    db_session.add(Customer(tenant_id=other.id, name="Z", email="z@example.com"))
    await db_session.commit()
    assert off_headers
    tuesday = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
    assert await email_autopilot.run_due(factory, tuesday) == 0  # wrong weekday
    monday = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)
    assert await email_autopilot.run_due(factory, monday) == 1  # only the workspace that turned it on
    campaigns = (await db_session.execute(select(Campaign).where(Campaign.tenant_id == due.id))).scalars().all()
    assert len(campaigns) == 1 and campaigns[0].name.startswith("Autopilot")
    assert await email_autopilot.run_due(factory, monday + timedelta(hours=2)) == 0  # already done this week
    # The beat schedule points at a task that really exists.
    entry = celery_app.conf.beat_schedule["email-autopilot"]
    assert entry["task"] in celery_app.tasks


def test_unfilled_bracketed_blanks_are_caught() -> None:
    result = email_checker.check_email("Offer for you", "Hi {{first_name}}, our [describe the offer] ends on [date]. Reply to this email if you would like to book a time with us this week.")
    assert any("[describe the offer]" in i["text"] and i["level"] == "bad" for i in result["issues"])
    assert not any("bracketed" in i["text"] for i in email_checker.check_email("Hello", "Hi {{first_name}}, a plain message with no blanks left in it at all, just text.")["issues"])


async def test_a_template_can_be_edited_but_not_renamed_onto_another_or_edited_by_another_workspace(client: AsyncClient) -> None:
    headers = await _owner(client, "tpl-co")
    other = await _owner(client, "tpl-other-co")
    one = (await client.post("/api/v1/email-templates", headers=headers, json={"name": "Reminder", "subject": "See you soon", "body": "Hi {{first_name}}"})).json()
    await client.post("/api/v1/email-templates", headers=headers, json={"name": "Offer", "subject": "An offer", "body": "Hello"})
    edited = await client.put(f"/api/v1/email-templates/{one['id']}", headers=headers, json={"name": " Reminder v2 ", "subject": "See you tomorrow", "body": "Hi {{first_name}}, tomorrow."})
    assert edited.status_code == 200 and edited.json()["name"] == "Reminder v2" and edited.json()["subject"] == "See you tomorrow"
    same_name = await client.put(f"/api/v1/email-templates/{one['id']}", headers=headers, json={"name": "Reminder v2", "subject": "s", "body": "b"})
    assert same_name.status_code == 200  # keeping its own name is fine
    clash = await client.put(f"/api/v1/email-templates/{one['id']}", headers=headers, json={"name": "Offer", "subject": "s", "body": "b"})
    assert clash.status_code == 409
    assert (await client.put(f"/api/v1/email-templates/{one['id']}", headers=other, json={"name": "Mine now", "subject": "s", "body": "b"})).status_code == 404
    listed = (await client.get("/api/v1/email-templates", headers=headers)).json()
    assert {t["name"] for t in listed} == {"Reminder v2", "Offer"}


async def test_sms_campaigns_are_gone(client: AsyncClient) -> None:
    headers = await _owner(client, "nosms-co")
    created = await client.post("/api/v1/campaigns", headers=headers, json={"name": "Texts", "channel": "sms", "sms_body": "hi"})
    assert created.status_code == 422 and "SMS campaigns are not available" in created.text
    assert (await client.post("/api/v1/campaigns/ai-sms", headers=headers, json={"instruction": "sale"})).status_code in (404, 405, 422)
