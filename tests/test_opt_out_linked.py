import uuid
from datetime import UTC, datetime

import pytest
from app.core.config import get_settings
from app.services import channels as channel_svc
from app.services import email_compliance, privacy
from app.services.opt_out import OPT_OUT_REPLY, is_stop_message
from app.worker import campaign_tasks
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Campaign,
    CampaignChannel,
    CampaignContact,
    CampaignContactStatus,
    CampaignStatus,
    ConsentRecord,
    ConversationChannel,
    Customer,
    DndEntry,
    Tenant,
)
from packages.database.models.email_suppression import EmailSuppression

PHONE = "+919800000001"
EMAIL = "priya@example.com"


async def _setup(db: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = Tenant(name="Shop", slug="shop-optout")
    db.add(tenant)
    await db.flush()
    customer = Customer(tenant_id=tenant.id, name="Priya", phone=PHONE, email=EMAIL)
    other = Customer(tenant_id=tenant.id, name="Other", phone="+919800000002", email="other@example.com")
    db.add_all([customer, other])
    await db.flush()
    for purpose in ("marketing", "call_back"):
        db.add(ConsentRecord(tenant_id=tenant.id, customer_id=customer.id, subject_phone=PHONE,
                             subject_email=EMAIL, purpose=purpose, status="granted", source="staff",
                             granted_at=datetime.now(UTC)))
    await db.commit()
    return tenant.id, customer.id


async def _state(db: AsyncSession, tenant_id: uuid.UUID) -> tuple[set[str], set[str]]:
    db.expire_all()
    numbers = {r.e164_number for r in (await db.execute(select(DndEntry).where(DndEntry.tenant_id == tenant_id))).scalars()}
    emails = {r.email for r in (await db.execute(select(EmailSuppression).where(EmailSuppression.tenant_id == tenant_id))).scalars()}
    return numbers, emails


def test_only_a_whole_stop_message_counts() -> None:
    for text in ("STOP", " stop ", "Stop all", "UNSUBSCRIBE", "Opt-out", "quit", "stop."):
        assert is_stop_message(text), text
    for text in ("cancel", "end", "please stop calling me at night", "stop by tomorrow", "", None, "4"):
        assert not is_stop_message(text), text


async def test_an_email_unsubscribe_also_stops_whatsapp_sms_and_calls(db_session: AsyncSession) -> None:
    tenant_id, _ = await _setup(db_session)
    await email_compliance.unsubscribe(db_session, tenant_id, EMAIL, source="one_click")
    await db_session.commit()
    numbers, emails = await _state(db_session, tenant_id)
    assert numbers == {PHONE} and emails == {EMAIL}  # the other customer is untouched
    statuses = {r.status for r in (await db_session.execute(select(ConsentRecord))).scalars()}
    assert statuses == {"withdrawn"}


async def test_a_phone_opt_out_also_stops_email(db_session: AsyncSession) -> None:
    tenant_id, _ = await _setup(db_session)
    result = await privacy.withdraw_consent(db_session, tenant_id, phone=PHONE, purposes=["marketing"], via="public_page")
    await db_session.commit()
    assert result["emails_suppressed"] == 1
    numbers, emails = await _state(db_session, tenant_id)
    assert numbers == {PHONE} and emails == {EMAIL}


async def test_withdrawing_only_a_recording_consent_leaves_marketing_alone(db_session: AsyncSession) -> None:
    tenant_id, _ = await _setup(db_session)
    await privacy.withdraw_consent(db_session, tenant_id, phone=PHONE, purposes=["recording"], via="staff")
    await db_session.commit()
    assert await _state(db_session, tenant_id) == (set(), set())


async def _thread(db: AsyncSession, tenant_id: uuid.UUID, customer_id: uuid.UUID, channel: ConversationChannel):
    conversation, _, _ = await channel_svc.ensure_thread(
        db, tenant_id, channel, f"{channel.value}-1", customer_id=customer_id, contact_address=PHONE
    )
    return conversation


@pytest.mark.parametrize("channel", [ConversationChannel.WHATSAPP, ConversationChannel.SMS])
async def test_replying_stop_opts_out_of_every_channel_and_the_ai_stays_quiet(
    db_session: AsyncSession, channel: ConversationChannel
) -> None:
    tenant_id, customer_id = await _setup(db_session)
    conversation = await _thread(db_session, tenant_id, customer_id, channel)
    outcome = await channel_svc.process_inbound_text(
        db_session, tenant_id=tenant_id, agent=None, conversation=conversation, text="STOP",
        provider_message_id="wamid.1", source=channel.value,
    )
    assert outcome.reply_text == OPT_OUT_REPLY
    assert await _state(db_session, tenant_id) == ({PHONE}, {EMAIL})


async def test_an_ordinary_message_with_the_word_cancel_does_not_opt_out(db_session: AsyncSession) -> None:
    tenant_id, customer_id = await _setup(db_session)
    conversation = await _thread(db_session, tenant_id, customer_id, ConversationChannel.WHATSAPP)
    outcome = await channel_svc.process_inbound_text(
        db_session, tenant_id=tenant_id, agent=None, conversation=conversation, text="cancel",
        provider_message_id="wamid.2", source="whatsapp",
    )
    assert outcome.reply_text is None
    assert await _state(db_session, tenant_id) == (set(), set())


async def test_whatsapp_campaign_skips_numbers_on_the_do_not_disturb_list(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "campaign_whatsapp_interval_seconds", 0)
    tenant_id, customer_id = await _setup(db_session)
    other = (await db_session.execute(select(Customer).where(Customer.name == "Other"))).scalar_one()
    other_id = other.id
    campaign = Campaign(tenant_id=tenant_id, name="Wa", channel=CampaignChannel.WHATSAPP,
                        status=CampaignStatus.RUNNING, whatsapp_template_name="promo")
    db_session.add(campaign)
    await db_session.flush()
    campaign_id = campaign.id
    for cid in (customer_id, other_id):
        db_session.add(CampaignContact(tenant_id=tenant_id, campaign_id=campaign_id, customer_id=cid))
    db_session.add(DndEntry(tenant_id=tenant_id, e164_number=PHONE, source="opt_out_linked"))
    await db_session.commit()
    sent: list[str] = []

    async def fake_send(db, campaign, account, customer):
        sent.append(customer.phone)

    monkeypatch.setattr(campaign_tasks, "_send_campaign_whatsapp", fake_send)
    await campaign_tasks._run_campaign(str(campaign_id))

    assert sent == ["+919800000002"]
    db_session.expire_all()
    rows = {r.customer_id: r for r in (await db_session.execute(select(CampaignContact))).scalars()}
    assert rows[customer_id].status == CampaignContactStatus.FAILED
    assert rows[customer_id].result == {"error": "number is on the do-not-disturb list", "skipped": True}
    assert rows[other_id].status == CampaignContactStatus.SENT
