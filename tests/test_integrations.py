import uuid

from app.core.config import get_settings
from app.integrations.credentials import get_sarvam_api_key, get_twilio_credentials
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Tenant


async def _signup(client: AsyncClient, *, slug: str, email: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "company_name": slug.title(),
            "tenant_slug": slug,
            "full_name": "Owner",
            "email": email,
            "password": "supersecret1",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _auth(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def test_credential_resolution_falls_back_to_platform_when_not_connected(
    db_session: AsyncSession,
) -> None:
    tenant = Tenant(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    db_session.add(tenant)
    await db_session.commit()

    settings = get_settings()
    twilio_creds = await get_twilio_credentials(db_session, tenant.id)
    assert twilio_creds.account_sid == settings.twilio_account_sid
    assert twilio_creds.auth_token == settings.twilio_auth_token

    sarvam_key = await get_sarvam_api_key(db_session, tenant.id)
    assert sarvam_key == settings.sarvam_api_key


async def test_connect_twilio_then_credentials_resolve_to_tenants_own(client: AsyncClient) -> None:
    tokens = await _signup(client, slug="byo-twilio-co", email="owner@byotwilio.com")

    connect_resp = await client.post(
        "/api/v1/integrations/twilio",
        headers=_auth(tokens),
        json={
            "account_sid": "ACtenantowned000000000000000000",
            "auth_token": "tenant-secret-token",
        },
    )
    assert connect_resp.status_code == 201, connect_resp.text
    body = connect_resp.json()
    assert body["provider"] == "twilio"
    assert body["config"]["account_sid"] == "ACtenantowned000000000000000000"
    assert "auth_token" not in body  # never echo the secret back

    list_resp = await client.get("/api/v1/integrations", headers=_auth(tokens))
    assert len(list_resp.json()) == 1

    disconnect_resp = await client.delete("/api/v1/integrations/twilio", headers=_auth(tokens))
    assert disconnect_resp.status_code == 204

    list_after = await client.get("/api/v1/integrations", headers=_auth(tokens))
    assert list_after.json() == []


async def test_connect_sarvam_key_is_tenant_isolated(client: AsyncClient) -> None:
    tokens_a = await _signup(client, slug="byo-sarvam-a", email="owner@byosarvam-a.com")
    tokens_b = await _signup(client, slug="byo-sarvam-b", email="owner@byosarvam-b.com")

    await client.post(
        "/api/v1/integrations/sarvam", headers=_auth(tokens_a), json={"api_key": "tenant-a-key"}
    )

    # Tenant B never connected anything — must not see tenant A's integration.
    list_as_b = await client.get("/api/v1/integrations", headers=_auth(tokens_b))
    assert list_as_b.json() == []


async def test_provision_phone_number_without_twilio_configured_fails_cleanly(
    client: AsyncClient,
) -> None:
    settings = get_settings()
    if settings.twilio_account_sid and settings.twilio_auth_token:
        return  # a real Twilio account is configured — this would actually buy a number

    tokens = await _signup(client, slug="no-twilio-co", email="owner@notwilio.com")

    resp = await client.post(
        "/api/v1/phone-numbers/provision",
        headers=_auth(tokens),
        json={"phone_number": "+15551234567"},
    )
    # No platform or tenant Twilio credentials configured — should fail with
    # a clear dependency error, not a 500.
    assert resp.status_code == 424


async def test_included_services_say_whether_the_platform_sarvam_key_covers_everyone(
    client, monkeypatch
):
    from app.core.config import get_settings

    tokens = await _signup_for_included(client)
    monkeypatch.setattr(get_settings(), "sarvam_api_key", "platform-key")
    on = await client.get(
        "/api/v1/integrations/included", headers={"Authorization": f"Bearer {tokens}"}
    )
    assert on.status_code == 200 and on.json() == {"sarvam": True}
    monkeypatch.setattr(get_settings(), "sarvam_api_key", "")
    off = await client.get(
        "/api/v1/integrations/included", headers={"Authorization": f"Bearer {tokens}"}
    )
    assert off.json() == {"sarvam": False}
    assert "platform-key" not in on.text  # only a yes or no, never the key


async def test_included_services_need_a_signed_in_user(client):
    assert (await client.get("/api/v1/integrations/included")).status_code in (401, 403)


async def _signup_for_included(client) -> str:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "company_name": "Inc Co",
            "tenant_slug": "inc-co",
            "full_name": "Owner",
            "email": "owner@inc-co.com",
            "password": "supersecret1",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["access_token"]
