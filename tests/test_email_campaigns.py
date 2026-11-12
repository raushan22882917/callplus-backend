import pytest
from app.core.config import get_settings
from app.integrations.channel_senders import ChannelSendError
from app.schemas.campaign import CampaignCreate
from app.services.cost_controls import SpendGuardError
from app.worker import campaign_tasks
from pydantic import ValidationError
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


def test_email_campaign_requires_subject_and_body() -> None:
    with pytest.raises(ValidationError):
        CampaignCreate(name="April note", channel=CampaignChannel.EMAIL)
    created = CampaignCreate(
        name="April note",
        channel=CampaignChannel.EMAIL,
        email_subject="  Hello {{first_name}}  ",
        email_body="  See you soon.  ",
        email_reply_to=" team@example.com ",
    )
    assert created.email_subject == "Hello {{first_name}}"
    assert created.email_body == "See you soon."
    assert created.email_reply_to == "team@example.com"
    CampaignCreate(name="Calls", channel=CampaignChannel.VOICE)


def test_render_fills_known_placeholders_and_drops_unknown_ones() -> None:
    customer = Customer(name="Priya Sharma", email="priya@example.com", phone="+9198")
    text = campaign_tasks.render_campaign_email(
        "Hi {{ first_name }}, {{name}} <{{email}}> {{phone}} {{unknown}}", customer
    )
    assert text == "Hi Priya, Priya Sharma <priya@example.com> +9198 "


async def _email_campaign(db: AsyncSession) -> tuple[Campaign, Customer, Customer]:
    tenant = Tenant(name="Mail Co", slug="mail-co")
    db.add(tenant)
    await db.flush()
    db.add(PrivacyProfile(tenant_id=tenant.id, legal_name="Mail Co Pvt Ltd", address="12 Park Road, Pune 411001"))
    campaign = Campaign(
        tenant_id=tenant.id,
        name="April note",
        channel=CampaignChannel.EMAIL,
        status=CampaignStatus.RUNNING,
        email_subject="Hello {{first_name}}",
        email_body="Hi {{name}},\nYour number is {{phone}}.",
        email_reply_to="reply@example.com",
    )
    db.add(campaign)
    await db.flush()
    with_email = Customer(
        tenant_id=tenant.id, name="Priya Sharma", email="priya@example.com", phone="+919800000001"
    )
    without_email = Customer(tenant_id=tenant.id, name="No Mail", phone="+919800000002")
    db.add_all([with_email, without_email])
    await db.flush()
    for customer in (with_email, without_email):
        db.add(CampaignContact(tenant_id=tenant.id, campaign_id=campaign.id, customer_id=customer.id))
    await db.commit()
    return campaign, with_email, without_email


async def test_email_campaign_sends_to_addresses_and_skips_the_rest(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "campaign_email_interval_seconds", 0)
    monkeypatch.setattr(settings, "campaign_retry_backoff_seconds", 0)
    campaign, with_email, without_email = await _email_campaign(db_session)
    with_id, without_id, campaign_id = with_email.id, without_email.id, campaign.id  # read before expire_all() below
    sent: list[dict] = []

    async def fake_send(db, campaign, customer):
        sent.append({"to": customer.email, "subject": campaign_tasks.render_campaign_email(campaign.email_subject, customer)})
        return {"to": customer.email, "subject": sent[-1]["subject"]}

    monkeypatch.setattr(campaign_tasks, "_send_campaign_email", fake_send)
    await campaign_tasks._run_campaign(str(campaign.id))

    assert sent == [{"to": "priya@example.com", "subject": "Hello Priya"}]
    db_session.expire_all()
    rows = {
        row.customer_id: row
        for row in (await db_session.execute(select(CampaignContact))).scalars().all()
    }
    assert rows[with_id].status == CampaignContactStatus.SENT
    assert rows[with_id].result["to"] == "priya@example.com"
    assert rows[without_id].status == CampaignContactStatus.FAILED
    assert rows[without_id].result["error"] == "customer has no email address"
    refreshed = await db_session.get(Campaign, campaign_id)
    assert refreshed is not None and refreshed.status == CampaignStatus.COMPLETED


async def test_email_send_renders_body_and_pauses_when_the_plan_is_used_up(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "email_api_key", "test-key")
    monkeypatch.setattr(settings, "campaign_email_interval_seconds", 0)
    campaign, with_email, _ = await _email_campaign(db_session)
    captured: dict = {}

    async def fake_send_email(*, to_email, subject, html_body, headers=None, tenant_id=None, credentials=None):
        captured.update(to=to_email, subject=subject, html=html_body, headers=headers)

    async def fake_reserve(db, tenant_id):
        return object()

    async def fake_sync(db, tenant_id):
        return 0

    monkeypatch.setattr(campaign_tasks, "send_email", fake_send_email)
    monkeypatch.setattr(campaign_tasks.plans, "reserve_email", fake_reserve)
    monkeypatch.setattr(campaign_tasks.plans, "sync_overage_invoice", fake_sync)

    campaign_id, tenant_id = campaign.id, campaign.tenant_id  # read before expire_all() below
    customer = await db_session.get(Customer, with_email.id)
    result = await campaign_tasks._send_campaign_email(db_session, campaign, customer)
    assert result["subject"] == "Hello Priya"
    assert captured["to"] == "priya@example.com"
    assert captured["headers"]["Reply-To"] == "reply@example.com"
    assert captured["headers"]["Message-ID"].startswith("<")
    assert "Priya Sharma" in captured["html"] and "<br>" in captured["html"]
    assert "&lt;" not in captured["html"] or "Hi Priya Sharma" in captured["html"]

    async def exhausted(db, tenant_id):
        raise SpendGuardError("plan_email_exhausted", "emails are used up")

    monkeypatch.setattr(campaign_tasks.plans, "reserve_email", exhausted)
    # A second customer who also has an email, so the pause leaves them pending.
    extra = Customer(tenant_id=campaign.tenant_id, name="Anita Rao", email="anita@example.com")
    db_session.add(extra)
    await db_session.flush()
    extra_id = extra.id
    db_session.add(CampaignContact(tenant_id=tenant_id, campaign_id=campaign_id, customer_id=extra_id))
    campaign.status = CampaignStatus.RUNNING
    await db_session.commit()

    await campaign_tasks._run_campaign(str(campaign_id))
    db_session.expire_all()
    paused = await db_session.get(Campaign, campaign_id)
    assert paused is not None and paused.status == CampaignStatus.PAUSED
    extra_row = (
        await db_session.execute(select(CampaignContact).where(CampaignContact.customer_id == extra_id))
    ).scalar_one()
    assert extra_row.status == CampaignContactStatus.PENDING


async def test_provider_failures_retry_and_missing_mail_config_does_not(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "campaign_max_send_attempts", 2)
    monkeypatch.setattr(settings, "campaign_retry_backoff_seconds", 0)
    monkeypatch.setattr(settings, "campaign_email_interval_seconds", 0)
    campaign, with_email, _ = await _email_campaign(db_session)
    with_id = with_email.id  # read before expire_all() below
    calls = {"n": 0}

    async def flaky(db, campaign, customer):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ChannelSendError("provider down")
        raise ValueError("email is not configured (set EMAIL_API_KEY or SMTP_HOST)")

    monkeypatch.setattr(campaign_tasks, "_send_campaign_email", flaky)
    await campaign_tasks._run_campaign(str(campaign.id))

    db_session.expire_all()
    row = (
        await db_session.execute(
            select(CampaignContact).where(CampaignContact.customer_id == with_id)
        )
    ).scalar_one()
    assert calls["n"] == 2
    assert row.status == CampaignContactStatus.FAILED
    assert row.result["attempts"] == 2
    assert "not configured" in row.result["error"]
