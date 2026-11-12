import uuid

import pytest
from app.core.config import get_settings
from app.integrations import channel_senders
from app.schemas.campaign import CampaignCreate
from app.services import follow_up as follow_up_svc
from app.services.sms_ai import parse_sms_draft
from app.worker import campaign_tasks
from httpx import AsyncClient
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
    DndEntry,
    Message,
    Tenant,
)


def test_an_sms_campaign_needs_a_message() -> None:
    with pytest.raises(ValidationError):
        CampaignCreate(name="Sale", channel=CampaignChannel.SMS)
    made = CampaignCreate(name="Sale", channel=CampaignChannel.SMS, sms_body="  Hi {{first_name}}, 20% off  ")
    assert made.sms_body == "Hi {{first_name}}, 20% off"


def test_the_ai_draft_is_read_from_json_and_capped() -> None:
    assert parse_sms_draft('<think>x</think>{"body": " Hello {{first_name}} "}') == "Hello {{first_name}}"
    assert parse_sms_draft("nothing") is None and parse_sms_draft('{"body": ""}') is None
    assert len(parse_sms_draft('{"body": "' + "a" * 900 + '"}')) == 640


async def _campaign(db: AsyncSession, phones: dict[str, str]) -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID]]:
    tenant = Tenant(name="Shop", slug="shop-sms")
    db.add(tenant)
    await db.flush()
    campaign = Campaign(tenant_id=tenant.id, name="Sale", channel=CampaignChannel.SMS, status=CampaignStatus.RUNNING,
                        sms_body="Hi {{first_name}}, 20% off today")
    db.add(campaign)
    await db.flush()
    ids = {}
    for name, phone in phones.items():
        customer = Customer(tenant_id=tenant.id, name=name, phone=phone)
        db.add(customer)
        await db.flush()
        ids[name] = customer.id
        db.add(CampaignContact(tenant_id=tenant.id, campaign_id=campaign.id, customer_id=customer.id))
    await db.commit()
    return tenant.id, campaign.id, ids


def _fake_sms(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    sent: list[dict] = []

    async def sms_from(db, tenant_id):
        return "+15550001111", None

    async def send_sms_message(db, tenant_id, *, to, from_, body, meter=True):
        sent.append({"to": to, "from": from_, "body": body})
        return f"SM{len(sent)}"

    monkeypatch.setattr(follow_up_svc, "_sms_from", sms_from)
    monkeypatch.setattr(channel_senders, "send_sms_message", send_sms_message)
    monkeypatch.setattr(get_settings(), "campaign_sms_interval_seconds", 0)
    return sent


async def test_sms_goes_out_personalised_with_a_stop_line_and_is_filed_in_the_inbox(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _fake_sms(monkeypatch)
    tenant_id, campaign_id, ids = await _campaign(db_session, {"Priya Sharma": "+919800000001"})
    campaign = await db_session.get(Campaign, campaign_id)
    customer = await db_session.get(Customer, ids["Priya Sharma"])

    result = await campaign_tasks._send_campaign_sms(db_session, campaign, customer)
    await db_session.commit()

    assert sent == [{"to": "+919800000001", "from": "+15550001111", "body": "Hi Priya, 20% off today Reply STOP to opt out."}]
    assert result["message_id"] == "SM1"
    stored = (await db_session.execute(select(Message).where(Message.tenant_id == tenant_id))).scalars().all()
    assert [m.text for m in stored] == [sent[0]["body"]]


async def test_sms_campaign_skips_opted_out_numbers_and_people_without_a_phone(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _fake_sms(monkeypatch)
    tenant_id, campaign_id, ids = await _campaign(
        db_session, {"Ok": "+919800000001", "Quiet": "+919800000002", "Nophone": ""}
    )
    db_session.add(DndEntry(tenant_id=tenant_id, e164_number="+919800000002", source="opt_out_linked"))
    await db_session.commit()
    ok_id, quiet_id, nophone_id = ids["Ok"], ids["Quiet"], ids["Nophone"]

    await campaign_tasks._run_campaign(str(campaign_id))

    assert [m["to"] for m in sent] == ["+919800000001"]
    db_session.expire_all()
    rows = {r.customer_id: r for r in (await db_session.execute(select(CampaignContact))).scalars()}
    assert rows[ok_id].status == CampaignContactStatus.SENT
    assert rows[quiet_id].result == {"error": "number is on the do-not-disturb list", "skipped": True}
    assert rows[nophone_id].result["error"] == "customer has no phone number"


async def _owner(client: AsyncClient, slug: str) -> dict:
    resp = await client.post("/api/v1/auth/signup", json={"company_name": slug.title(), "tenant_slug": slug,
                             "full_name": "Owner", "email": f"owner@{slug}.com", "password": "supersecret1"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_api_enrols_only_people_with_a_phone_who_have_not_opted_out_and_checks_sms_is_ready(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _owner(client, "sms-api")
    tenant = (await db_session.execute(select(Tenant).where(Tenant.slug == "sms-api"))).scalar_one()
    tenant_id = tenant.id
    db_session.add_all([
        Customer(tenant_id=tenant_id, name="Ok", phone="+919800000001"),
        Customer(tenant_id=tenant_id, name="Quiet", phone="+919800000002"),
        Customer(tenant_id=tenant_id, name="Mail only", email="m@example.com"),
        DndEntry(tenant_id=tenant_id, e164_number="+919800000002", source="opt_out_linked"),
    ])
    await db_session.commit()

    created = await client.post("/api/v1/campaigns", headers=headers, json={"name": "Sale", "channel": "sms", "sms_body": "Hello"})
    assert created.status_code == 201, created.text
    assert created.json()["sms_body"] == "Hello"
    campaign_id = created.json()["id"]
    enrolled = await client.post(f"/api/v1/campaigns/{campaign_id}/enroll", headers=headers, json={})
    assert enrolled.status_code == 201 and len(enrolled.json()) == 1

    refused = await client.post(f"/api/v1/campaigns/{campaign_id}/launch", headers=headers)
    assert refused.status_code == 422 and "SMS" in refused.json()["detail"]  # SMS is not turned on for this workspace
    blank = await client.patch(f"/api/v1/campaigns/{campaign_id}", headers=headers, json={"sms_body": "  "})
    assert blank.status_code == 422
