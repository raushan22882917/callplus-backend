import base64
import hashlib
import hmac
import json
import time
import uuid

import pytest
from app.api.v1.email_compliance import verify_svix
from app.core.config import get_settings
from app.services import email_compliance as compliance
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
    ConsentRecord,
    Customer,
    PrivacyProfile,
    Tenant,
)
from packages.database.models.email_suppression import EmailSuppression

ADDRESS = "12 Park Road, Pune 411001"


async def _tenant(db: AsyncSession, slug: str = "mail-co", *, address: str | None = ADDRESS) -> uuid.UUID:
    tenant = Tenant(name="Mail Co", slug=slug)
    db.add(tenant)
    await db.flush()
    db.add(PrivacyProfile(tenant_id=tenant.id, legal_name="Mail Co Pvt Ltd", address=address))
    await db.commit()
    return tenant.id


async def _campaign(db: AsyncSession, tenant_id: uuid.UUID, emails: list[str]) -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    campaign = Campaign(
        tenant_id=tenant_id,
        name="Promo",
        channel=CampaignChannel.EMAIL,
        status=CampaignStatus.RUNNING,
        email_subject="Hello {{first_name}}",
        email_body="Big sale.",
    )
    db.add(campaign)
    await db.flush()
    ids: dict[str, uuid.UUID] = {}
    for email in emails:
        customer = Customer(tenant_id=tenant_id, name=email.split("@")[0].title(), email=email)
        db.add(customer)
        await db.flush()
        ids[email] = customer.id
        db.add(CampaignContact(tenant_id=tenant_id, campaign_id=campaign.id, customer_id=customer.id))
    await db.commit()
    return campaign.id, ids


def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "campaign_email_interval_seconds", 0)
    monkeypatch.setattr(settings, "campaign_retry_backoff_seconds", 0)


async def _contacts(db: AsyncSession) -> dict[uuid.UUID, CampaignContact]:
    db.expire_all()
    rows = (await db.execute(select(CampaignContact))).scalars().all()
    return {row.customer_id: row for row in rows}


# ---- tokens and footer --------------------------------------------------------------------------


def test_unsubscribe_token_round_trips_and_rejects_tampering() -> None:
    tenant_id = uuid.uuid4()
    token = compliance.make_token(tenant_id, "  Priya@Example.com ")
    assert compliance.parse_token(token) == (tenant_id, "priya@example.com")
    other = compliance.make_token(uuid.uuid4(), "priya@example.com")
    forged = token.rsplit(".", 1)[0] + "." + other.rsplit(".", 1)[1]
    assert compliance.parse_token(forged) is None
    assert compliance.parse_token("not-a-token") is None
    assert compliance.parse_token("") is None


def test_one_click_headers_follow_rfc_8058() -> None:
    headers = compliance.unsubscribe_headers("https://api.example.com/u/abc")
    assert headers["List-Unsubscribe"] == "<https://api.example.com/u/abc>"
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_footer_escapes_the_business_details() -> None:
    sender = compliance.Sender(name="A & B <Ltd>", address="1 Road")
    html = compliance.footer_html(sender, "https://x.test/u?a=1&b=2")
    assert "A &amp; B &lt;Ltd&gt;" in html and "&amp;b=2" in html
    assert "Unsubscribe: https://x.test/u" in compliance.footer_text(sender, "https://x.test/u")


# ---- the campaign send --------------------------------------------------------------------------


async def test_campaign_email_carries_footer_and_one_click_headers(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id = await _tenant(db_session)
    campaign_id, ids = await _campaign(db_session, tenant_id, ["priya@example.com"])
    captured: dict = {}

    async def fake_send_email(*, to_email, subject, html_body, headers=None, tenant_id=None, credentials=None):
        captured.update(html=html_body, headers=headers, tenant_id=tenant_id)

    async def fake_reserve(db, tid):
        return object()

    async def fake_sync(db, tid):
        return 0

    monkeypatch.setattr(get_settings(), "email_api_key", "k")
    monkeypatch.setattr(campaign_tasks, "send_email", fake_send_email)
    monkeypatch.setattr(campaign_tasks.plans, "reserve_email", fake_reserve)
    monkeypatch.setattr(campaign_tasks.plans, "sync_overage_invoice", fake_sync)
    campaign = await db_session.get(Campaign, campaign_id)
    customer = await db_session.get(Customer, ids["priya@example.com"])

    await campaign_tasks._send_campaign_email(db_session, campaign, customer)

    link = captured["headers"]["List-Unsubscribe"].strip("<>")
    assert captured["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert compliance.parse_token(link.rsplit("/", 1)[1]) == (tenant_id, "priya@example.com")
    assert ADDRESS in captured["html"] and "Mail Co Pvt Ltd" in captured["html"] and link in captured["html"]
    assert captured["tenant_id"] == str(tenant_id)


async def test_campaign_email_refuses_to_send_without_a_postal_address(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id = await _tenant(db_session, address=None)
    campaign_id, ids = await _campaign(db_session, tenant_id, ["priya@example.com"])
    monkeypatch.setattr(get_settings(), "email_api_key", "k")
    campaign = await db_session.get(Campaign, campaign_id)
    customer = await db_session.get(Customer, ids["priya@example.com"])
    with pytest.raises(ValueError, match="postal address"):
        await campaign_tasks._send_campaign_email(db_session, campaign, customer)


async def test_suppressed_addresses_are_skipped_not_sent(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fast(monkeypatch)
    tenant_id = await _tenant(db_session)
    emails = ["ok@example.com", "gone@example.com", "bounced@example.com"]
    campaign_id, ids = await _campaign(db_session, tenant_id, emails)
    await compliance.suppress(db_session, tenant_id, "Gone@Example.com", reason="unsubscribed", source="unsubscribe_link")
    await compliance.suppress(db_session, tenant_id, "bounced@example.com", reason="bounced", source="provider_resend")
    await db_session.commit()
    sent: list[str] = []

    async def fake_send(db, campaign, customer):
        sent.append(customer.email)
        return {"to": customer.email}

    monkeypatch.setattr(campaign_tasks, "_send_campaign_email", fake_send)
    await campaign_tasks._run_campaign(str(campaign_id))

    assert sent == ["ok@example.com"]
    rows = await _contacts(db_session)
    assert rows[ids["ok@example.com"]].status == CampaignContactStatus.SENT
    assert rows[ids["gone@example.com"]].result == {"error": "recipient unsubscribed", "skipped": True}
    assert rows[ids["bounced@example.com"]].result["error"] == "address bounced earlier"


async def test_consent_switch_limits_sends_to_people_who_agreed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fast(monkeypatch)
    tenant_id = await _tenant(db_session)
    campaign_id, ids = await _campaign(db_session, tenant_id, ["yes@example.com", "silent@example.com"])
    profile = (await db_session.execute(select(PrivacyProfile).where(PrivacyProfile.tenant_id == tenant_id))).scalar_one()
    profile.email_marketing_requires_consent = True
    db_session.add(
        ConsentRecord(
            tenant_id=tenant_id, subject_email="yes@example.com", purpose="marketing", status="granted",
            source="staff", granted_at=profile.created_at,
        )
    )
    await db_session.commit()
    sent: list[str] = []

    async def fake_send(db, campaign, customer):
        sent.append(customer.email)
        return {}

    monkeypatch.setattr(campaign_tasks, "_send_campaign_email", fake_send)
    await campaign_tasks._run_campaign(str(campaign_id))

    assert sent == ["yes@example.com"]
    rows = await _contacts(db_session)
    assert rows[ids["silent@example.com"]].result["error"] == "no marketing consent on record"


# ---- the public unsubscribe link ----------------------------------------------------------------


async def test_unsubscribe_page_shows_a_button_and_only_the_post_unsubscribes(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant_id = await _tenant(db_session)
    db_session.add(
        ConsentRecord(
            tenant_id=tenant_id, subject_email="priya@example.com", purpose="marketing", status="granted",
            source="staff", granted_at=(await db_session.get(Tenant, tenant_id)).created_at,
        )
    )
    await db_session.commit()
    token = compliance.make_token(tenant_id, "priya@example.com")

    page = await client.get(f"/api/v1/email/unsubscribe/{token}")  # a mail scanner opening the link
    assert page.status_code == 200 and "<form" in page.text and "priya@example.com" in page.text
    assert await compliance.get_suppression(db_session, tenant_id, "priya@example.com") is None

    done = await client.post(f"/api/v1/email/unsubscribe/{token}", content="List-Unsubscribe=One-Click",
                             headers={"content-type": "application/x-www-form-urlencoded"})
    assert done.status_code == 200 and "unsubscribed" in done.text.lower()
    db_session.expire_all()
    row = await compliance.get_suppression(db_session, tenant_id, "priya@example.com")
    assert row is not None and row.reason == "unsubscribed" and row.source == "one_click"
    consent = (await db_session.execute(select(ConsentRecord))).scalar_one()
    assert consent.status == "withdrawn"

    again = await client.post(f"/api/v1/email/unsubscribe/{token}")  # repeating it is harmless
    assert again.status_code == 200
    assert len((await db_session.execute(select(EmailSuppression))).scalars().all()) == 1


async def test_a_bad_unsubscribe_link_is_a_404_and_changes_nothing(client: AsyncClient, db_session: AsyncSession) -> None:
    assert (await client.get("/api/v1/email/unsubscribe/nonsense")).status_code == 404
    assert (await client.post("/api/v1/email/unsubscribe/nonsense")).status_code == 404
    assert (await db_session.execute(select(EmailSuppression))).first() is None


# ---- staff API ----------------------------------------------------------------------------------


async def _owner(client: AsyncClient, slug: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"company_name": slug.title(), "tenant_slug": slug, "full_name": "Owner",
              "email": f"owner@{slug}.com", "password": "supersecret1"},
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_suppression_list_staff_can_add_but_not_undo_an_unsubscribe(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _owner(client, "supp-co")
    added = await client.post("/api/v1/email/suppressions", headers=headers, json={"email": "Boss@Example.com"})
    assert added.status_code == 201 and added.json()["email"] == "boss@example.com"
    assert added.json()["reason"] == "manual"
    tenant = (await db_session.execute(select(Tenant).where(Tenant.slug == "supp-co"))).scalar_one()
    await compliance.unsubscribe(db_session, tenant.id, "gone@example.com", source="unsubscribe_link")
    await db_session.commit()

    listed = (await client.get("/api/v1/email/suppressions", headers=headers)).json()
    by_email = {row["email"]: row for row in listed}
    assert set(by_email) == {"boss@example.com", "gone@example.com"}
    assert (await client.delete(f"/api/v1/email/suppressions/{by_email['gone@example.com']['id']}", headers=headers)).status_code == 409
    assert (await client.delete(f"/api/v1/email/suppressions/{by_email['boss@example.com']['id']}", headers=headers)).status_code == 204
    assert (await client.get("/api/v1/email/suppressions", headers=headers)).json()[0]["email"] == "gone@example.com"
    assert (await client.get("/api/v1/email/suppressions")).status_code == 401


async def test_recording_consent_is_refused_for_a_suppressed_address(client: AsyncClient) -> None:
    headers = await _owner(client, "consent-co")
    ok = await client.post("/api/v1/email/consents", headers=headers, json={"email": "a@example.com", "name": "A"})
    assert ok.status_code == 201
    await client.post("/api/v1/email/suppressions", headers=headers, json={"email": "b@example.com"})
    blocked = await client.post("/api/v1/email/consents", headers=headers, json={"email": "b@example.com"})
    assert blocked.status_code == 409


async def test_launch_needs_a_postal_address_and_enrol_skips_suppressed_people(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "email_api_key", "k")
    headers = await _owner(client, "launch-co")
    tenant = (await db_session.execute(select(Tenant).where(Tenant.slug == "launch-co"))).scalar_one()
    tenant_id = tenant.id
    for name, email in (("Keep", "keep@example.com"), ("Gone", "gone@example.com")):
        db_session.add(Customer(tenant_id=tenant_id, name=name, email=email))
    await compliance.suppress(db_session, tenant_id, "gone@example.com", reason="complaint", source="provider_resend")
    await db_session.commit()

    created = await client.post(
        "/api/v1/campaigns", headers=headers,
        json={"name": "Promo", "channel": "email", "email_subject": "Hi", "email_body": "Sale"},
    )
    assert created.status_code == 201, created.text
    campaign_id = created.json()["id"]
    enrolled = await client.post(f"/api/v1/campaigns/{campaign_id}/enroll", headers=headers, json={"audience": "with_email"})
    assert enrolled.status_code == 201 and len(enrolled.json()) == 1

    refused = await client.post(f"/api/v1/campaigns/{campaign_id}/launch", headers=headers)
    assert refused.status_code == 422 and "postal address" in refused.json()["detail"]


# ---- bounce and complaint events ----------------------------------------------------------------

SECRET_BYTES = b"a-test-signing-key-of-some-length"
SECRET = "whsec_" + base64.b64encode(SECRET_BYTES).decode()


def _signed(body: bytes, *, stamp: int | None = None, secret_bytes: bytes = SECRET_BYTES) -> dict:
    stamp = stamp if stamp is not None else int(time.time())
    digest = hmac.new(secret_bytes, f"msg_1.{stamp}.".encode() + body, hashlib.sha256).digest()
    return {"svix-id": "msg_1", "svix-timestamp": str(stamp), "svix-signature": "v1," + base64.b64encode(digest).decode()}


def test_svix_signature_checks() -> None:
    body = b'{"type":"email.bounced"}'
    assert verify_svix(SECRET, _signed(body), body)
    assert not verify_svix(SECRET, _signed(body), body + b" ")  # body changed
    assert not verify_svix(SECRET, _signed(body, secret_bytes=b"another-key-entirely-here!!"), body)
    assert not verify_svix(SECRET, _signed(body, stamp=int(time.time()) - 3600), body)  # replayed
    assert not verify_svix("", _signed(body), body)  # secret not configured: refuse everything
    assert not verify_svix(SECRET, {}, body)


async def test_resend_bounce_and_complaint_suppress_for_the_tagged_workspace(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "email_resend_webhook_secret", SECRET)
    tenant_id = await _tenant(db_session)

    def event(kind: str, to: str, **data) -> bytes:
        return json.dumps({"type": kind, "data": {"to": [to], "tags": {"tenant": str(tenant_id)}, **data}}).encode()

    for body in (
        event("email.bounced", "hard@example.com", bounce={"type": "Permanent"}),
        event("email.complained", "angry@example.com"),
        event("email.bounced", "full@example.com", bounce={"type": "Transient"}),
        event("email.delivered", "fine@example.com"),
    ):
        resp = await client.post("/api/v1/email/events/resend", content=body, headers=_signed(body))
        assert resp.status_code == 200, resp.text

    db_session.expire_all()
    rows = {r.email: r.reason for r in (await db_session.execute(select(EmailSuppression))).scalars().all()}
    assert rows == {"hard@example.com": "bounced", "angry@example.com": "complaint"}

    untagged = json.dumps({"type": "email.complained", "data": {"to": ["x@example.com"]}}).encode()
    assert (await client.post("/api/v1/email/events/resend", content=untagged, headers=_signed(untagged))).json() == {"suppressed": 0}
    forged = event("email.complained", "victim@example.com")
    assert (await client.post("/api/v1/email/events/resend", content=forged, headers={**_signed(forged), "svix-id": "other"})).status_code == 403


async def test_provider_webhooks_refuse_everything_until_a_secret_is_set(client: AsyncClient) -> None:
    body = b'{"type":"email.complained","data":{}}'
    assert (await client.post("/api/v1/email/events/resend", content=body, headers=_signed(body))).status_code == 403
    assert (await client.post("/api/v1/email/events/brevo", json={"event": "spam"})).status_code == 403
    assert (await client.post("/api/v1/email/events/brevo", json={"event": "spam"}, headers={"Authorization": "Bearer "})).status_code == 403


async def test_brevo_events_need_the_bearer_token(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "email_brevo_webhook_token", "brevo-secret-token")
    tenant_id = await _tenant(db_session)
    auth = {"Authorization": "Bearer brevo-secret-token"}
    payload = {"event": "hard_bounce", "email": "Hard@Example.com", "tags": [f"tenant_{tenant_id}"]}

    assert (await client.post("/api/v1/email/events/brevo", json=payload, headers={"Authorization": "Bearer wrong"})).status_code == 403
    assert (await client.post("/api/v1/email/events/brevo", json=payload, headers=auth)).json() == {"suppressed": 1}
    assert (await client.post("/api/v1/email/events/brevo", json={**payload, "event": "delivered"}, headers=auth)).json() == {"suppressed": 0}
    assert (await client.post("/api/v1/email/events/brevo", json={**payload, "event": "soft_bounce"}, headers=auth)).json() == {"suppressed": 0}
    db_session.expire_all()
    row = await compliance.get_suppression(db_session, tenant_id, "hard@example.com")
    assert row is not None and row.reason == "bounced" and row.source == "provider_brevo"
