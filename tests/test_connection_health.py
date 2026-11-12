"""Connections are only "working" when the provider says so just now. Provider calls are mocked."""

import uuid

import pytest
from app.core.crypto import encrypt_secret
from app.integrations import sarvam as sarvam_client
from app.services import connection_health as health
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Integration, IntegrationCredential, IntegrationStatus, Tenant, WhatsAppAccount
from tests.test_oauth import seed_connection
from tests.test_integrations import _auth, _signup


@pytest.fixture(autouse=True)
def _fresh_cache():
    health._cache.clear()
    yield
    health._cache.clear()


async def _tenant(db: AsyncSession) -> uuid.UUID:
    tenant = Tenant(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    db.add(tenant)
    await db.commit()
    return tenant.id


async def _integration(db: AsyncSession, tenant_id: uuid.UUID, provider: str, secret: str, config: dict | None = None) -> None:
    row = Integration(tenant_id=tenant_id, provider=provider, status=IntegrationStatus.CONNECTED, config=config or {})
    db.add(row)
    await db.flush()
    db.add(IntegrationCredential(tenant_id=tenant_id, integration_id=row.id, encrypted_secret=encrypt_secret(secret)))
    await db.commit()


def _by_id(payload: dict) -> dict[str, dict]:
    return {r["id"]: r for r in payload["results"]}


# ---- telling "no" from "could not ask" ------------------------------------------------------------------


def test_a_refusal_is_failed_and_an_unreachable_provider_is_only_unknown() -> None:
    assert health.classify("Sarvam refused this API key. Copy it again.")[0] == health.FAILED
    assert health.classify("We could not reach Sarvam. Try again in a moment.")[0] == health.UNKNOWN
    assert health.classify("Could not reach ElevenLabs: timeout")[0] == health.UNKNOWN
    assert health.classify("We could not check your Twilio account right now.")[0] == health.UNKNOWN


# ---- the checks -----------------------------------------------------------------------------------------


async def test_a_saved_key_is_working_only_if_the_provider_accepts_it_now(db_session: AsyncSession, monkeypatch) -> None:
    tenant_id = await _tenant(db_session)
    await _integration(db_session, tenant_id, "sarvam", "sk-live")
    seen: list[str] = []

    async def accepted(key: str) -> None:
        seen.append(key)

    monkeypatch.setattr(sarvam_client, "check_api_key", accepted)
    result = _by_id(await health.check_all(db_session, tenant_id))
    assert result["sarvam"]["state"] == health.WORKING and seen == ["sk-live"]

    health.forget(tenant_id)

    async def revoked(key: str) -> None:
        raise sarvam_client.SarvamAPIError("Sarvam refused this API key. Copy it again from the Sarvam dashboard.")

    monkeypatch.setattr(sarvam_client, "check_api_key", revoked)
    result = _by_id(await health.check_all(db_session, tenant_id))
    assert result["sarvam"]["state"] == health.FAILED and "refused" in result["sarvam"]["message"]


async def test_twilio_is_checked_with_the_workspaces_own_account_and_a_suspended_one_fails(db_session: AsyncSession, monkeypatch) -> None:
    tenant_id = await _tenant(db_session)
    await _integration(db_session, tenant_id, "twilio", "tok", {"account_sid": "ACabc"})
    calls: list[tuple[str, str]] = []

    def check(creds):
        calls.append((creds.account_sid, creds.auth_token))
        return False, "Your Twilio account is suspended or closed."

    monkeypatch.setattr(health, "check_account", check)
    result = _by_id(await health.check_all(db_session, tenant_id))
    assert calls == [("ACabc", "tok")]
    assert result["twilio"]["state"] == health.FAILED


async def test_nothing_is_reported_for_services_the_workspace_has_not_connected(db_session: AsyncSession) -> None:
    tenant_id = await _tenant(db_session)
    assert (await health.check_all(db_session, tenant_id))["results"] == []


async def test_one_check_blowing_up_does_not_hide_the_others(db_session: AsyncSession, monkeypatch) -> None:
    tenant_id = await _tenant(db_session)
    await _integration(db_session, tenant_id, "sarvam", "sk")
    await _integration(db_session, tenant_id, "twilio", "tok", {"account_sid": "ACabc"})

    async def boom(key: str) -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(sarvam_client, "check_api_key", boom)
    monkeypatch.setattr(health, "check_account", lambda creds: (True, "Twilio accepted these details."))
    result = _by_id(await health.check_all(db_session, tenant_id))
    assert result["sarvam"]["state"] == health.UNKNOWN and "kaboom" not in result["sarvam"]["message"]
    assert result["twilio"]["state"] == health.WORKING


async def test_a_slow_provider_is_unknown_not_failed(db_session: AsyncSession, monkeypatch) -> None:
    tenant_id = await _tenant(db_session)
    await _integration(db_session, tenant_id, "sarvam", "sk")
    monkeypatch.setattr(health, "CHECK_TIMEOUT", 0.05)

    async def slow(key: str) -> None:
        import asyncio

        await asyncio.sleep(1)

    monkeypatch.setattr(sarvam_client, "check_api_key", slow)
    assert _by_id(await health.check_all(db_session, tenant_id))["sarvam"]["state"] == health.UNKNOWN


async def test_whatsapp_is_working_only_if_every_number_is(db_session: AsyncSession, monkeypatch) -> None:
    tenant_id = await _tenant(db_session)
    for n in ("111", "222"):
        db_session.add(WhatsAppAccount(tenant_id=tenant_id, phone_number_id=n, waba_id="w1", display_phone_number=f"+91{n}",
                                       encrypted_access_token=encrypt_secret(f"tok-{n}")))
    await db_session.commit()
    outcomes = {"tok-111": (health.WORKING, "ok"), "tok-222": (health.FAILED, "Meta did not accept the access token.")}

    def fake(waba, phone, token):
        async def probe():
            return outcomes[token]

        return probe

    monkeypatch.setattr(health, "_whatsapp", fake)
    result = _by_id(await health.check_all(db_session, tenant_id))
    assert result["whatsapp"]["state"] == health.FAILED and "Meta did not accept" in result["whatsapp"]["message"]


async def test_a_sign_in_connection_is_working_when_the_provider_accepts_its_token(db_session: AsyncSession, monkeypatch) -> None:
    tenant_id = await _tenant(db_session)
    await seed_connection(db_session, tenant_id, "slack")
    await seed_connection(db_session, tenant_id, "google")
    seen: list[tuple[str, str]] = []

    def fake(provider, token, extra):
        async def probe():
            seen.append((provider, token))
            return (health.FAILED, "Slack no longer accepts this connection.") if provider == "slack" else (health.WORKING, "ok")

        return probe

    monkeypatch.setattr(health, "_oauth_probe", fake)
    result = _by_id(await health.check_all(db_session, tenant_id))
    assert result["oauth:slack"]["state"] == health.FAILED and result["oauth:google"]["state"] == health.WORKING
    assert ("google", "a-1") in seen  # the stored token, decrypted, never sent anywhere else


async def test_a_connection_marked_revoked_is_failed_without_asking_anyone(db_session: AsyncSession, monkeypatch) -> None:
    tenant_id = await _tenant(db_session)
    conn = await seed_connection(db_session, tenant_id, "hubspot")
    conn.status, conn.last_error = "revoked", "HubSpot revoked access."
    await db_session.commit()
    monkeypatch.setattr(health, "_oauth_probe", lambda *a: (_ for _ in ()).throw(AssertionError("must not call the provider")))
    result = _by_id(await health.check_all(db_session, tenant_id))
    assert result["oauth:hubspot"] == {**result["oauth:hubspot"], "state": health.FAILED, "message": "HubSpot revoked access."}


# ---- caching --------------------------------------------------------------------------------------------


async def test_results_are_kept_for_a_while_and_forgetting_asks_again(db_session: AsyncSession, monkeypatch) -> None:
    tenant_id = await _tenant(db_session)
    await _integration(db_session, tenant_id, "sarvam", "sk")
    calls = {"n": 0}

    async def accepted(key: str) -> None:
        calls["n"] += 1

    monkeypatch.setattr(sarvam_client, "check_api_key", accepted)
    first = await health.check_all(db_session, tenant_id)
    second = await health.check_all(db_session, tenant_id)
    forced = await health.check_all(db_session, tenant_id, force=True)  # asked again too soon: still the kept answer
    assert (first["cached"], second["cached"], forced["cached"]) == (False, True, True) and calls["n"] == 1
    health.forget(tenant_id)
    assert (await health.check_all(db_session, tenant_id))["cached"] is False and calls["n"] == 2


async def test_check_again_works_once_the_short_wait_is_over(db_session: AsyncSession, monkeypatch) -> None:
    tenant_id = await _tenant(db_session)
    await _integration(db_session, tenant_id, "sarvam", "sk")
    calls = {"n": 0}

    async def accepted(key: str) -> None:
        calls["n"] += 1

    monkeypatch.setattr(sarvam_client, "check_api_key", accepted)
    monkeypatch.setattr(health, "FORCE_MIN_SECONDS", 0)
    await health.check_all(db_session, tenant_id)
    await health.check_all(db_session, tenant_id, force=True)
    assert calls["n"] == 2


# ---- the route ------------------------------------------------------------------------------------------


async def test_the_route_needs_sign_in_and_only_shows_this_workspaces_connections(client: AsyncClient, monkeypatch) -> None:
    assert (await client.get("/api/v1/integrations/health")).status_code == 401
    monkeypatch.setattr(health, "check_account", lambda creds: (True, "ok"))
    tokens = await _signup(client, slug="health-a", email="a@health-demo.example.com")
    other = await _signup(client, slug="health-b", email="b@health-demo.example.com")
    saved = await client.post("/api/v1/integrations/twilio", headers=_auth(tokens), json={"account_sid": "ACa", "auth_token": "t"})
    assert saved.status_code == 201, saved.text
    mine = (await client.get("/api/v1/integrations/health", headers=_auth(tokens))).json()
    theirs = (await client.get("/api/v1/integrations/health", headers=_auth(other))).json()
    assert [r["id"] for r in mine["results"]] == ["twilio"] and mine["results"][0]["state"] == "working"
    assert theirs["results"] == []


async def test_disconnecting_clears_the_kept_answer_so_the_page_cannot_show_a_stale_connection(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setattr(health, "check_account", lambda creds: (True, "ok"))
    tokens = await _signup(client, slug="health-c", email="c@health-demo.example.com")
    await client.post("/api/v1/integrations/twilio", headers=_auth(tokens), json={"account_sid": "ACc", "auth_token": "t"})
    assert len((await client.get("/api/v1/integrations/health", headers=_auth(tokens))).json()["results"]) == 1
    assert (await client.delete("/api/v1/integrations/twilio", headers=_auth(tokens))).status_code == 204
    assert (await client.get("/api/v1/integrations/health", headers=_auth(tokens))).json()["results"] == []
