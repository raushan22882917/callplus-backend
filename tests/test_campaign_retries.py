import pytest
from app.core.config import get_settings
from app.integrations.whatsapp import WhatsAppAPIError
from app.worker import campaign_tasks
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Campaign,
    CampaignChannel,
    CampaignContact,
    CampaignContactStatus,
    CampaignStatus,
    Customer,
    Tenant,
)


async def _make_campaign(db: AsyncSession, phones: list[str]) -> tuple[Campaign, dict[str, str]]:
    tenant = Tenant(name="Retry Co", slug="retry-co")
    db.add(tenant)
    await db.flush()
    campaign = Campaign(
        tenant_id=tenant.id,
        name="Blast",
        channel=CampaignChannel.WHATSAPP,
        status=CampaignStatus.RUNNING,
        whatsapp_template_name="promo",
    )
    db.add(campaign)
    await db.flush()
    by_phone: dict[str, str] = {}
    for phone in phones:
        customer = Customer(tenant_id=tenant.id, name=phone, phone=phone)
        db.add(customer)
        await db.flush()
        db.add(
            CampaignContact(tenant_id=tenant.id, campaign_id=campaign.id, customer_id=customer.id)
        )
        by_phone[phone] = str(customer.id)
    await db.commit()
    return campaign, by_phone


async def test_transient_failures_are_retried_and_permanent_ones_are_not(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "campaign_max_send_attempts", 3)
    monkeypatch.setattr(settings, "campaign_retry_backoff_seconds", 0)

    campaign, _ = await _make_campaign(db_session, ["+911", "+912", "+913"])
    calls: dict[str, int] = {}

    async def fake_send(db, campaign, whatsapp_account, customer) -> None:
        calls[customer.phone] = calls.get(customer.phone, 0) + 1
        if customer.phone == "+911" and calls[customer.phone] < 3:
            raise WhatsAppAPIError("rate limited")  # transient: succeeds on attempt 3
        if customer.phone == "+912":
            raise WhatsAppAPIError("still down")  # exhausts all attempts
        if customer.phone == "+913":
            raise ValueError("bad template")  # config error: never retried

    monkeypatch.setattr(campaign_tasks, "_send_campaign_whatsapp", fake_send)
    await campaign_tasks._run_campaign(str(campaign.id))

    assert calls == {"+911": 3, "+912": 3, "+913": 1}

    db_session.expire_all()
    contacts = (await db_session.execute(select(CampaignContact))).scalars().all()
    customers = {
        c.id: c.phone for c in (await db_session.execute(select(Customer))).scalars().all()
    }
    result = {customers[c.customer_id]: c for c in contacts}
    assert result["+911"].status == CampaignContactStatus.SENT
    assert result["+912"].status == CampaignContactStatus.FAILED
    assert result["+912"].result["attempts"] == 3
    assert result["+913"].status == CampaignContactStatus.FAILED
    assert result["+913"].result["attempts"] == 1
