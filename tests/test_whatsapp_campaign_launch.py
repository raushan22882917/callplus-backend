"""A WhatsApp campaign is checked when it is launched, not contact by contact afterwards."""

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_secret
from packages.database.models import Tenant, WhatsAppAccount, WhatsAppTemplate
from tests.test_email_compliance import _owner


async def _campaign(client: AsyncClient, headers: dict, template: str | None) -> str:
    body = {"name": "Diwali", "channel": "whatsapp"}
    if template:
        body["whatsapp_template_name"] = template
    created = await client.post("/api/v1/campaigns", headers=headers, json=body)
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def test_launch_needs_a_connected_number_and_an_approved_template(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    headers = await _owner(client, "wa-launch-co")
    tenant_id = (await db_session.execute(select(Tenant).where(Tenant.slug == "wa-launch-co"))).scalar_one().id

    cid = await _campaign(client, headers, "diwali_offer")
    no_number = await client.post(f"/api/v1/campaigns/{cid}/launch", headers=headers)
    assert no_number.status_code == 422 and "Connect a WhatsApp number" in no_number.json()["detail"]

    db_session.add(WhatsAppAccount(tenant_id=tenant_id, phone_number_id="1", waba_id="w", display_phone_number="+911",
                                   encrypted_access_token=encrypt_secret("t")))
    await db_session.commit()

    no_template = await client.post(f"/api/v1/campaigns/{await _campaign(client, headers, None)}/launch", headers=headers)
    assert no_template.status_code == 422 and "template" in no_template.json()["detail"]

    db_session.add(WhatsAppTemplate(tenant_id=tenant_id, name="diwali_offer", language="en", category="MARKETING",
                                    status="PENDING", purpose="custom", body="Hi {{name}}", variables=["name"]))
    await db_session.commit()
    pending = await client.post(f"/api/v1/campaigns/{cid}/launch", headers=headers)
    assert pending.status_code == 422 and "not approved" in pending.json()["detail"]


async def _approved(db: AsyncSession, tenant_id, name: str, category: str) -> None:
    db.add(WhatsAppTemplate(tenant_id=tenant_id, name=name, language="en", category=category, status="APPROVED",
                            purpose="custom", body="Hi {{name}}", variables=["name"]))
    await db.commit()


async def test_marketing_goes_only_to_numbers_that_opted_in(client: AsyncClient, db_session: AsyncSession) -> None:
    from packages.database.models import Customer

    headers = await _owner(client, "wa-optin-co")
    tenant_id = (await db_session.execute(select(Tenant).where(Tenant.slug == "wa-optin-co"))).scalar_one().id
    await _approved(db_session, tenant_id, "diwali_offer", "MARKETING")
    for name, phone in (("Asha", "+919811111111"), ("Ravi", "+919822222222")):
        db_session.add(Customer(tenant_id=tenant_id, name=name, phone=phone))
    await db_session.commit()

    cid = await _campaign(client, headers, "diwali_offer")
    nobody = await client.post(f"/api/v1/campaigns/{cid}/enroll", headers=headers, json={"audience": "with_phone"})
    assert nobody.status_code == 422 and "agreed to WhatsApp marketing" in nobody.json()["detail"]

    recorded = await client.post("/api/v1/whatsapp/consents", headers=headers, json={
        "people": [{"phone": "+91 98111 11111", "name": "Asha"}, {"phone": "not-a-number"}]})
    assert recorded.status_code == 201
    assert recorded.json()["recorded"] == 1 and recorded.json()["skipped"][0]["reason"] == "not a phone number"

    enrolled = await client.post(f"/api/v1/campaigns/{cid}/enroll", headers=headers, json={"audience": "with_phone"})
    assert enrolled.status_code == 201 and len(enrolled.json()) == 1  # Asha only; Ravi never agreed


async def test_a_utility_template_does_not_need_marketing_opt_in(client: AsyncClient, db_session: AsyncSession) -> None:
    from packages.database.models import Customer

    headers = await _owner(client, "wa-utility-co")
    tenant_id = (await db_session.execute(select(Tenant).where(Tenant.slug == "wa-utility-co"))).scalar_one().id
    await _approved(db_session, tenant_id, "appointment_reminder", "UTILITY")
    db_session.add(Customer(tenant_id=tenant_id, name="Asha", phone="+919811111111"))
    await db_session.commit()
    cid = await _campaign(client, headers, "appointment_reminder")
    enrolled = await client.post(f"/api/v1/campaigns/{cid}/enroll", headers=headers, json={"audience": "with_phone"})
    assert enrolled.status_code == 201 and len(enrolled.json()) == 1


async def test_opt_in_list_counts_people_and_needs_the_campaign_permission(client: AsyncClient) -> None:
    headers = await _owner(client, "wa-list-co")
    await client.post("/api/v1/whatsapp/consents", headers=headers,
                      json={"people": [{"phone": "+919811111111", "name": "Asha"}, {"phone": "+919822222222"}]})
    listed = (await client.get("/api/v1/whatsapp/consents", headers=headers)).json()
    assert listed["total"] == 2 and {r["phone"] for r in listed["recent"]} == {"+919811111111", "+919822222222"}
    assert (await client.get("/api/v1/whatsapp/consents")).status_code == 401
