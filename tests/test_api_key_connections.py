"""Connecting with a pasted API token or key: checked with the provider first, saved encrypted, used by the feature,
and never sent back or logged."""

import httpx
import pytest
from app.core.crypto import decrypt_secret
from app.integrations import email_client, email_providers
from app.integrations import credentials as creds_module
from app.integrations.oauth.tokens import get_connection, get_valid_access_token
from app.services import connection_health as health
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Integration
from tests.test_call_control import _auth, _signup
from tests.test_oauth import MockProviders, creds, providers, tenant_id_of  # noqa: F401 - fixtures

SECRET = "pat-na1-very-secret-token-123"


@pytest.fixture(autouse=True)
def _fresh():
    health._cache.clear()


async def _workspace(client: AsyncClient, slug: str):
    tokens = await _signup(client, slug=slug, email=f"o@{slug}-demo.example.com")
    return tokens


def hubspot_ok(mock: MockProviders) -> None:
    mock.add("GET", "https://api.hubapi.com/account-info/v3/details", {"portalId": 4242, "accountType": "STANDARD"})


# ---- HubSpot, Shopify, Slack by token --------------------------------------------------------------------


async def test_a_hubspot_token_is_checked_saved_encrypted_and_usable_like_a_sign_in(client: AsyncClient, db_session: AsyncSession, providers: MockProviders) -> None:
    tokens = await _workspace(client, "tok-hub")
    hubspot_ok(providers)
    resp = await client.post("/api/v1/oauth/hubspot/token", headers=_auth(tokens), json={"token": SECRET})
    assert resp.status_code == 201, resp.text
    assert SECRET not in resp.text  # never sent back
    tenant_id = await tenant_id_of(db_session, "tok-hub")
    conn = await get_connection(db_session, tenant_id, "hubspot")
    assert conn is not None and conn.account_label == "HubSpot portal 4242" and conn.extra["manual"] is True
    assert conn.encrypted_access_token != SECRET and SECRET not in conn.encrypted_access_token  # encrypted at rest
    assert decrypt_secret(conn.encrypted_access_token) == SECRET and conn.encrypted_refresh_token is None
    assert await get_valid_access_token(db_session, tenant_id, "hubspot") == SECRET  # what lead push / alerts call
    assert providers.calls_to("GET", "https://api.hubapi.com/account-info")[0].headers["authorization"] == f"Bearer {SECRET}"
    raw = (await db_session.execute(text("select count(*) from oauth_connections where encrypted_access_token like :t"), {"t": f"%{SECRET}%"})).scalar_one()
    assert raw == 0


async def test_a_token_the_provider_refuses_is_not_saved(client: AsyncClient, db_session: AsyncSession, providers: MockProviders) -> None:
    tokens = await _workspace(client, "tok-bad")
    providers.add("GET", "https://api.hubapi.com/account-info/v3/details", {"message": "bad"}, status=401)
    resp = await client.post("/api/v1/oauth/hubspot/token", headers=_auth(tokens), json={"token": SECRET})
    assert resp.status_code == 422 and "did not accept" in resp.text and SECRET not in resp.text
    assert await get_connection(db_session, await tenant_id_of(db_session, "tok-bad"), "hubspot") is None


async def test_a_provider_that_cannot_be_reached_is_reported_as_such_not_as_a_bad_token(client: AsyncClient, providers: MockProviders) -> None:
    tokens = await _workspace(client, "tok-down")
    providers.add("GET", "https://api.hubapi.com/account-info/v3/details", {}, status=503)
    resp = await client.post("/api/v1/oauth/hubspot/token", headers=_auth(tokens), json={"token": SECRET})
    assert resp.status_code == 502 and "could not reach" in resp.text.lower()


async def test_slack_needs_a_bot_token_and_records_the_team(client: AsyncClient, db_session: AsyncSession, providers: MockProviders) -> None:
    tokens = await _workspace(client, "tok-slack")
    user_token = await client.post("/api/v1/oauth/slack/token", headers=_auth(tokens), json={"token": "xoxp-12345678-user"})
    assert user_token.status_code == 422 and "xoxb-" in user_token.text
    providers.add("POST", "https://slack.com/api/auth.test", {"ok": True, "team": "Lotus Salon", "team_id": "T1", "user_id": "U1"})
    ok = await client.post("/api/v1/oauth/slack/token", headers=_auth(tokens), json={"token": "xoxb-12345678-bot"})
    assert ok.status_code == 201, ok.text
    conn = await get_connection(db_session, await tenant_id_of(db_session, "tok-slack"), "slack")
    assert conn.account_label == "Lotus Salon" and conn.extra["team_id"] == "T1" and conn.extra["manual"] is True


async def test_shopify_needs_a_real_store_and_a_token_it_accepts(client: AsyncClient, db_session: AsyncSession, providers: MockProviders) -> None:
    tokens = await _workspace(client, "tok-shop")
    no_store = await client.post("/api/v1/oauth/shopify/token", headers=_auth(tokens), json={"token": "shpat_12345678"})
    assert no_store.status_code == 422
    providers.add("GET", "https://lotus.myshopify.com/admin/api/", {"shop": {"name": "Lotus", "email": "o@lotus.example.com"}})
    ok = await client.post("/api/v1/oauth/shopify/token", headers=_auth(tokens), json={"token": "shpat_12345678", "shop": "lotus"})
    assert ok.status_code == 201, ok.text
    conn = await get_connection(db_session, await tenant_id_of(db_session, "tok-shop"), "shopify")
    assert conn.extra["shop"] == "lotus.myshopify.com" and conn.external_account_id == "lotus.myshopify.com"
    assert providers.calls_to("GET", "https://lotus.myshopify.com")[0].headers["x-shopify-access-token"] == "shpat_12345678"


async def test_it_works_even_when_the_platform_has_no_app_for_that_provider(client: AsyncClient, providers: MockProviders, monkeypatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "hubspot_client_id", "")
    monkeypatch.setattr(get_settings(), "hubspot_client_secret", "")
    tokens = await _workspace(client, "tok-noapp")
    listed = {p["provider"]: p for p in (await client.get("/api/v1/oauth/providers", headers=_auth(tokens))).json()}
    assert listed["hubspot"]["configured"] is False and listed["hubspot"]["token_supported"] is True
    assert listed["hubspot"]["token_label"] and "Private Apps" in listed["hubspot"]["token_steps"]
    hubspot_ok(providers)
    assert (await client.post("/api/v1/oauth/hubspot/token", headers=_auth(tokens), json={"token": SECRET})).status_code == 201
    after = {p["provider"]: p for p in (await client.get("/api/v1/oauth/providers", headers=_auth(tokens))).json()}
    assert after["hubspot"]["connected"] is True and after["hubspot"]["via_token"] is True
    assert after["google"]["token_supported"] is False


async def test_providers_without_a_token_option_refuse_politely_and_a_signed_out_caller_cannot_try(client: AsyncClient) -> None:
    tokens = await _workspace(client, "tok-no")
    google = await client.post("/api/v1/oauth/google/token", headers=_auth(tokens), json={"token": "ya29.12345678"})
    assert google.status_code == 400
    assert (await client.post("/api/v1/oauth/hubspot/token", json={"token": SECRET})).status_code == 401
    assert (await client.post("/api/v1/oauth/nope/token", headers=_auth(tokens), json={"token": SECRET})).status_code == 404


async def test_disconnecting_a_pasted_token_does_not_ask_the_provider_to_revoke_it(client: AsyncClient, db_session: AsyncSession, providers: MockProviders, creds) -> None:
    tokens = await _workspace(client, "tok-disc")
    hubspot_ok(providers)
    await client.post("/api/v1/oauth/hubspot/token", headers=_auth(tokens), json={"token": SECRET})
    providers.add("DELETE", "https://api.hubapi.com/oauth/v1/refresh-tokens/", {})
    assert (await client.delete("/api/v1/oauth/hubspot", headers=_auth(tokens))).status_code == 204
    assert providers.calls_to("DELETE", "https://api.hubapi.com") == []
    assert await get_connection(db_session, await tenant_id_of(db_session, "tok-disc"), "hubspot") is None


# ---- the email sending key -------------------------------------------------------------------------------


_REAL_CLIENT = httpx.AsyncClient  # the real class, kept before any test replaces it


def _mock_httpx(monkeypatch, module, handler) -> None:
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=transport, **kw))


def resend_domains(*domains: tuple[str, str], status: int = 200, body: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if body is not None or status != 200:
            return httpx.Response(status, json=body or {})
        return httpx.Response(200, json={"data": [{"name": n, "status": s} for n, s in domains]})

    return handler


async def test_resend_must_know_and_have_verified_the_senders_domain(monkeypatch) -> None:
    _mock_httpx(monkeypatch, email_providers, resend_domains(("shop.example.com", "verified"), ("new.example.com", "pending")))
    assert await email_providers.check_sender("resend", "key-12345678", "hi@shop.example.com") is None
    with pytest.raises(email_providers.EmailProviderError, match="not a domain on this Resend account"):
        await email_providers.check_sender("resend", "key-12345678", "hi@other.example.com")
    with pytest.raises(email_providers.EmailProviderError, match="not verified in Resend yet"):
        await email_providers.check_sender("resend", "key-12345678", "hi@new.example.com")


async def test_resend_refuses_a_bad_key_but_accepts_a_send_only_key(monkeypatch) -> None:
    _mock_httpx(monkeypatch, email_providers, resend_domains(status=401, body={"name": "validation_error"}))
    with pytest.raises(email_providers.EmailProviderError, match="did not accept this key"):
        await email_providers.check_sender("resend", "key-12345678", "hi@shop.example.com")
    _mock_httpx(monkeypatch, email_providers, resend_domains(status=401, body={"name": "restricted_api_key"}))
    note = await email_providers.check_sender("resend", "key-12345678", "hi@shop.example.com")
    assert note and "could not be checked" in note


async def test_brevo_must_have_the_sender_active(monkeypatch) -> None:
    body = {"senders": [{"email": "hi@shop.example.com", "active": True}, {"email": "off@shop.example.com", "active": False}]}
    _mock_httpx(monkeypatch, email_providers, lambda r: httpx.Response(200, json=body))
    assert await email_providers.check_sender("brevo", "key-12345678", "HI@shop.example.com") is None
    with pytest.raises(email_providers.EmailProviderError, match="not a sender"):
        await email_providers.check_sender("brevo", "key-12345678", "x@shop.example.com")
    with pytest.raises(email_providers.EmailProviderError, match="not active"):
        await email_providers.check_sender("brevo", "key-12345678", "off@shop.example.com")
    _mock_httpx(monkeypatch, email_providers, lambda r: httpx.Response(401, json={}))
    with pytest.raises(email_providers.EmailProviderError, match="did not accept this key"):
        await email_providers.check_sender("brevo", "key-12345678", "hi@shop.example.com")


async def test_an_unreachable_email_provider_is_a_clear_message(monkeypatch) -> None:
    def boom(request):
        raise httpx.ConnectError("down")

    _mock_httpx(monkeypatch, email_providers, boom)
    with pytest.raises(email_providers.EmailProviderError, match="could not reach Resend"):
        await email_providers.check_sender("resend", "key-12345678", "hi@shop.example.com")


async def test_the_workspaces_email_key_is_checked_saved_encrypted_and_then_resolved_for_sending(client: AsyncClient, db_session: AsyncSession, monkeypatch) -> None:
    _mock_httpx(monkeypatch, email_providers, resend_domains(("shop.example.com", "verified")))
    tokens = await _workspace(client, "mail-key")
    key = "re_live_abcdefghijkl"
    bad = await client.post("/api/v1/integrations/email-sender", headers=_auth(tokens), json={"provider": "resend", "api_key": key, "from_email": "hi@elsewhere.example.com"})
    assert bad.status_code == 422 and key not in bad.text
    ok = await client.post("/api/v1/integrations/email-sender", headers=_auth(tokens),
                           json={"provider": "resend", "api_key": key, "from_email": "Hi@Shop.example.com", "from_name": " Lotus Salon "})
    assert ok.status_code == 201, ok.text
    assert key not in ok.text and ok.json()["config"] == {"provider": "resend", "from_email": "hi@shop.example.com", "from_name": "Lotus Salon"}
    tenant_id = await tenant_id_of(db_session, "mail-key")
    stored = (await db_session.execute(text("select encrypted_secret from integration_credentials"))).scalars().all()
    assert stored and all(key not in s for s in stored)
    sender = await creds_module.get_email_sender(db_session, tenant_id)
    assert (sender.provider, sender.api_key, sender.from_email, sender.from_name) == ("resend", key, "hi@shop.example.com", "Lotus Salon")
    # another workspace has none
    other = await _workspace(client, "mail-other")
    assert await creds_module.get_email_sender(db_session, await tenant_id_of(db_session, "mail-other")) is None
    assert (await client.post("/api/v1/integrations/email-sender", json={"provider": "resend", "api_key": key, "from_email": "a@b.co"})).status_code == 401
    del other


async def test_customer_email_goes_out_through_the_workspaces_own_account_and_from_its_address(monkeypatch) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(url=str(request.url), auth=request.headers.get("authorization"), body=request.read().decode())
        return httpx.Response(200, json={})

    _mock_httpx(monkeypatch, email_client, handler)
    creds_ = email_providers.EmailApiCredentials("resend", "re_own_key", "hi@shop.example.com", "Lotus Salon")
    await email_client.send_email(to_email="priya@example.com", subject="Hello", html_body="<p>x</p>", tenant_id="t-1", credentials=creds_)
    assert seen["url"] == "https://api.resend.com/emails" and seen["auth"] == "Bearer re_own_key"
    assert '"from":"Lotus Salon <hi@shop.example.com>"' in seen["body"].replace(" ", "") or "Lotus Salon <hi@shop.example.com>" in seen["body"]
    brevo = email_providers.EmailApiCredentials("brevo", "br_own_key", "hi@shop.example.com", None)
    await email_client.send_email(to_email="priya@example.com", subject="Hello", html_body="<p>x</p>", credentials=brevo)
    assert seen["url"] == "https://api.brevo.com/v3/smtp/email" and '"hi@shop.example.com"' in seen["body"]


async def test_a_workspace_with_its_own_key_can_send_a_campaign_without_the_platform_mailer(db_session: AsyncSession, monkeypatch) -> None:
    from app.core.config import get_settings
    from app.core.crypto import encrypt_secret
    from app.worker import campaign_tasks
    from packages.database.models import Campaign, CampaignChannel, CampaignStatus, Customer, IntegrationCredential, IntegrationStatus, PrivacyProfile, Tenant

    monkeypatch.setattr(get_settings(), "email_api_key", "")
    monkeypatch.setattr(get_settings(), "smtp_host", "")
    tenant = Tenant(name="Shop", slug="shop-ownkey")
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(PrivacyProfile(tenant_id=tenant.id, legal_name="Shop", address="1 Road, Pune"))
    row = Integration(tenant_id=tenant.id, provider="email_sender", status=IntegrationStatus.CONNECTED,
                      config={"provider": "resend", "from_email": "hi@shop.example.com", "from_name": "Shop"})
    db_session.add(row)
    await db_session.flush()
    db_session.add(IntegrationCredential(tenant_id=tenant.id, integration_id=row.id, encrypted_secret=encrypt_secret("re_own")))
    campaign = Campaign(tenant_id=tenant.id, name="c", channel=CampaignChannel.EMAIL, status=CampaignStatus.RUNNING, email_subject="Hi", email_body="Body")
    customer = Customer(tenant_id=tenant.id, name="Priya", email="priya@example.com")
    db_session.add_all([campaign, customer])
    await db_session.commit()
    sent: dict = {}

    async def fake_send(*, to_email, subject, html_body, headers=None, tenant_id=None, credentials=None):
        sent.update(to=to_email, credentials=credentials)

    async def fake_reserve(db, tid):
        return object()

    async def fake_sync(db, tid):
        return 0

    monkeypatch.setattr(campaign_tasks, "send_email", fake_send)
    monkeypatch.setattr(campaign_tasks.plans, "reserve_email", fake_reserve)
    monkeypatch.setattr(campaign_tasks.plans, "sync_overage_invoice", fake_sync)
    await campaign_tasks._send_campaign_email(db_session, campaign, customer)
    assert sent["to"] == "priya@example.com" and sent["credentials"].api_key == "re_own" and sent["credentials"].from_email == "hi@shop.example.com"


async def test_the_page_can_verify_the_email_key_live_and_see_it_go_stale(db_session: AsyncSession, monkeypatch) -> None:
    from app.core.crypto import encrypt_secret
    from packages.database.models import IntegrationCredential, IntegrationStatus, Tenant

    tenant = Tenant(name="Shop", slug="shop-health")
    db_session.add(tenant)
    await db_session.flush()
    row = Integration(tenant_id=tenant.id, provider="email_sender", status=IntegrationStatus.CONNECTED,
                      config={"provider": "brevo", "from_email": "hi@shop.example.com"})
    db_session.add(row)
    await db_session.flush()
    db_session.add(IntegrationCredential(tenant_id=tenant.id, integration_id=row.id, encrypted_secret=encrypt_secret("br_own")))
    await db_session.commit()
    _mock_httpx(monkeypatch, email_providers, lambda r: httpx.Response(200, json={"senders": [{"email": "hi@shop.example.com", "active": True}]}))
    results = {r["id"]: r for r in (await health.check_all(db_session, tenant.id))["results"]}
    assert results["email_sender"]["state"] == "working"
    health.forget(tenant.id)
    _mock_httpx(monkeypatch, email_providers, lambda r: httpx.Response(401, json={}))
    results = {r["id"]: r for r in (await health.check_all(db_session, tenant.id))["results"]}
    assert results["email_sender"]["state"] == "failed" and "did not accept" in results["email_sender"]["message"]
    assert (await db_session.execute(select(Integration).where(Integration.provider == "email_sender"))).scalar_one()
