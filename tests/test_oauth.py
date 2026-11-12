"""Per-tenant OAuth framework: state/PKCE, callback, refresh, isolation, login.
Every provider HTTP call is mocked (httpx.MockTransport); no network."""

import asyncio
import hashlib
import hmac
import time
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from app.core.config import get_settings
from app.core.crypto import decrypt_secret
from app.integrations.oauth import http as oauth_http
from app.integrations.oauth import state as st
from app.integrations.oauth.errors import OAuthError
from app.integrations.oauth.providers import Identity, TokenResult
from app.integrations.oauth.tokens import get_connection, get_valid_access_token, upsert_connection
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Tenant
from packages.database.models.oauth import OAuthConnection, OAuthState
from packages.database.models.platform import IntegrationConnector
from tests.test_call_control import _auth, _signup

PUBLIC = "https://api.test"
FRONT = "http://front.test"


# ---------------------------------------------------------------- helpers


class MockProviders:
    """Routes outbound httpx requests by (method, url prefix)."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, object]] = []
        self.calls: list[httpx.Request] = []

    def add(self, method: str, prefix: str, json=None, status: int = 200, handler=None) -> None:
        self.routes.append(
            (method, prefix, handler or (lambda req, j=json, s=status: httpx.Response(s, json=j)))
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        for method, prefix, fn in self.routes:
            if request.method == method and str(request.url).startswith(prefix):
                return fn(request)
        return httpx.Response(599, json={"error": f"unmocked {request.method} {request.url}"})

    def calls_to(self, method: str, prefix: str) -> list[httpx.Request]:
        return [c for c in self.calls if c.method == method and str(c.url).startswith(prefix)]


@pytest.fixture
def providers():
    mock = MockProviders()
    oauth_http.set_transport(httpx.MockTransport(mock))
    yield mock
    oauth_http.set_transport(None)


@pytest.fixture
def creds(monkeypatch):
    s = get_settings()
    for name, value in {
        "public_base_url": PUBLIC,
        "frontend_base_url": FRONT,
        "google_client_id": "gid",
        "google_client_secret": "gsecret",
        "microsoft_client_id": "mid",
        "microsoft_client_secret": "msecret",
        "salesforce_client_id": "sfid",
        "salesforce_client_secret": "sfsecret",
        "hubspot_client_id": "hid",
        "hubspot_client_secret": "hsecret",
        "zoho_client_id": "zid",
        "zoho_client_secret": "zsecret",
        "slack_client_id": "sid",
        "slack_client_secret": "ssecret",
        "meta_app_id": "metaid",
        "meta_app_secret": "metasecret",
        "shopify_client_id": "shid",
        "shopify_client_secret": "shsecret",
    }.items():
        monkeypatch.setattr(s, name, value)
    return s


def state_of(url: str) -> str:
    return parse_qs(urlparse(url).query)["state"][0]


async def authorize(client: AsyncClient, tokens: dict, provider: str, **body) -> str:
    resp = await client.post(f"/api/v1/oauth/{provider}/authorize", headers=_auth(tokens), json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["authorization_url"]


async def callback(client: AsyncClient, provider: str, **params) -> httpx.Response:
    return await client.get(
        f"/api/v1/oauth/{provider}/callback", params=params, follow_redirects=False
    )


def redirect_query(resp: httpx.Response) -> dict[str, str]:
    assert resp.status_code == 302, resp.text
    return {k: v[0] for k, v in parse_qs(urlparse(resp.headers["location"]).query).items()}


def google_mocks(mock: MockProviders, *, expires_in: int = 3600) -> None:
    mock.add(
        "POST",
        "https://oauth2.googleapis.com/token",
        {
            "access_token": "g-access-1",
            "refresh_token": "g-refresh-1",
            "expires_in": expires_in,
            "scope": "openid email https://www.googleapis.com/auth/calendar.events",
        },
    )
    mock.add(
        "GET",
        "https://openidconnect.googleapis.com/v1/userinfo",
        {"email": "owner@acme.test", "sub": "g-123", "name": "Owner"},
    )


async def connect_google(client, tokens, mock) -> httpx.Response:
    google_mocks(mock)
    url = await authorize(client, tokens, "google")
    return await callback(client, "google", code="the-code", state=state_of(url))


async def seed_connection(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    provider: str = "google",
    *,
    expires_in: int | None = 3600,
    refresh: str | None = "r-1",
    extra: dict | None = None,
) -> OAuthConnection:
    conn = await upsert_connection(
        db,
        tenant_id=tenant_id,
        user_id=None,
        provider=provider,
        token=TokenResult(
            access_token="a-1", refresh_token=refresh, expires_in=expires_in, extra=extra or {}
        ),
        identity=Identity(label="owner@acme.test", email="owner@acme.test", external_id="x1"),
        requested_scopes=["openid"],
    )
    await db.commit()
    return conn


async def tenant_id_of(db: AsyncSession, slug: str) -> uuid.UUID:
    return (await db.execute(select(Tenant.id).where(Tenant.slug == slug))).scalar_one()


# ---------------------------------------------------------------- state + PKCE


def test_pkce_challenge_is_s256_of_verifier() -> None:
    import base64

    verifier, challenge = st.generate_pkce()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert challenge == expected.decode()
    assert st.generate_pkce()[0] != verifier


def test_state_signature_expiry_and_tamper() -> None:
    token = st.sign_state(
        provider="google", purpose="connect", nonce="n1", tenant_id="t", user_id="u",
        return_url="/integrations/hub",
    )
    claims = st.read_state(token)
    assert claims["tenant_id"] == "t" and claims["user_id"] == "u" and claims["provider"] == "google"

    expired = st.sign_state(
        provider="google", purpose="connect", nonce="n1", tenant_id="t", user_id="u",
        return_url="/x", now=datetime.now(UTC) - timedelta(minutes=30),
    )
    with pytest.raises(OAuthError) as exc:
        st.read_state(expired)
    assert exc.value.code == "state_expired"

    head, payload, sig = token.split(".")
    tampered = f"{head}.{payload}.{sig[:-2]}AA"
    with pytest.raises(OAuthError) as exc:
        st.read_state(tampered)
    assert exc.value.code == "invalid_state"

    forged = jwt.encode({"typ": "oauth_state", "nonce": "n", "exp": time.time() + 60}, "wrong", algorithm="HS256")
    with pytest.raises(OAuthError):
        st.read_state(forged)


def test_return_url_cannot_redirect_off_site() -> None:
    assert st.safe_return_url("/integrations/hub?x=1") == "/integrations/hub?x=1"
    for bad in ("https://evil.test", "//evil.test", "/\\evil.test", "evil", None, "/a://b"):
        assert st.safe_return_url(bad) == st.DEFAULT_RETURN_URL


async def test_state_is_single_use_and_carries_verifier(db_session: AsyncSession) -> None:
    token, challenge, nonce = await st.create_state(
        db_session, provider="google", purpose="connect", tenant_id="t", user_id="u",
        return_url="/integrations/hub", use_pkce=True,
    )
    row = await db_session.get(OAuthState, nonce)
    assert row.encrypted_code_verifier and row.encrypted_code_verifier != challenge
    claims = await st.consume_state(db_session, token, provider="google", purpose="connect")
    assert claims.code_verifier and claims.tenant_id == "t"
    with pytest.raises(OAuthError) as exc:
        await st.consume_state(db_session, token, provider="google", purpose="connect")
    assert exc.value.code == "state_replayed"


async def test_state_bound_to_provider_and_purpose(db_session: AsyncSession) -> None:
    token, _, _ = await st.create_state(
        db_session, provider="google", purpose="connect", tenant_id="t", user_id="u", return_url=None
    )
    with pytest.raises(OAuthError) as exc:
        await st.consume_state(db_session, token, provider="hubspot", purpose="connect")
    assert exc.value.code == "invalid_state"
    with pytest.raises(OAuthError):
        await st.consume_state(db_session, token, provider="google", purpose="login")
    # the mismatched attempts did not burn it
    assert (await st.consume_state(db_session, token, provider="google", purpose="connect")).tenant_id == "t"


async def test_expired_state_row_is_rejected(db_session: AsyncSession) -> None:
    old = datetime.now(UTC) - timedelta(minutes=30)
    token, _, _ = await st.create_state(
        db_session, provider="google", purpose="connect", tenant_id="t", user_id="u",
        return_url=None, now=old,
    )
    with pytest.raises(OAuthError) as exc:
        await st.consume_state(db_session, token, provider="google", purpose="connect")
    assert exc.value.code == "state_expired"


# ---------------------------------------------------------------- providers + authorize


async def test_provider_not_configured(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "google_client_id", "")
    tokens = await _signup(client, slug="np-co", email="o@npco.com")
    listing = await client.get("/api/v1/oauth/providers", headers=_auth(tokens))
    assert listing.status_code == 200
    by_key = {p["provider"]: p for p in listing.json()}
    assert set(by_key) == {"google", "gmail", "microsoft", "salesforce", "hubspot", "zoho", "slack", "shopify"}
    assert by_key["google"]["configured"] is False and by_key["google"]["connected"] is False
    resp = await client.post("/api/v1/oauth/google/authorize", headers=_auth(tokens), json={})
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "provider_not_configured"
    unknown = await client.post("/api/v1/oauth/nope/authorize", headers=_auth(tokens), json={})
    assert unknown.status_code == 404


async def test_authorize_requires_auth_and_builds_google_url(client: AsyncClient, creds) -> None:
    assert (await client.post("/api/v1/oauth/google/authorize", json={})).status_code == 401
    tokens = await _signup(client, slug="az-co", email="o@azco.com")
    url = await authorize(client, tokens, "google", return_url="/integrations/hub", options=["sheets"])
    q = parse_qs(urlparse(url).query)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert q["client_id"] == ["gid"]
    assert q["redirect_uri"] == [f"{PUBLIC}/api/v1/oauth/google/callback"]
    assert q["access_type"] == ["offline"] and q["code_challenge_method"] == ["S256"]
    assert "https://www.googleapis.com/auth/spreadsheets" in q["scope"][0]
    assert "calendar.events" in q["scope"][0]
    claims = st.read_state(q["state"][0])
    assert claims["provider"] == "google" and claims["return_url"] == "/integrations/hub"
    bad = await client.post(
        "/api/v1/oauth/google/authorize", headers=_auth(tokens), json={"options": ["nope"]}
    )
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == "invalid_option"


async def test_meta_secret_falls_back_to_whatsapp_secret(monkeypatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "meta_app_secret", "")
    monkeypatch.setattr(s, "meta_whatsapp_app_secret", "wa-secret")
    assert s.meta_oauth_secret == "wa-secret"


# ---------------------------------------------------------------- callback


async def test_callback_happy_path_stores_encrypted_tokens(
    client: AsyncClient, creds, providers: MockProviders, db_session: AsyncSession
) -> None:
    tokens = await _signup(client, slug="cb-co", email="o@cbco.com")
    url = await authorize(client, tokens, "google")
    challenge = parse_qs(urlparse(url).query)["code_challenge"][0]
    google_mocks(providers)
    resp = await callback(client, "google", code="the-code", state=state_of(url))
    assert resp.headers["location"] == f"{FRONT}/integrations/hub?connected=google"

    # PKCE: the verifier sent to the token endpoint hashes to the challenge in the URL
    sent = parse_qs(providers.calls_to("POST", "https://oauth2.googleapis.com/token")[0].content.decode())
    import base64

    digest = base64.urlsafe_b64encode(hashlib.sha256(sent["code_verifier"][0].encode()).digest())
    assert digest.rstrip(b"=").decode() == challenge
    assert sent["redirect_uri"] == [f"{PUBLIC}/api/v1/oauth/google/callback"]
    assert sent["code"] == ["the-code"] and sent["client_secret"] == ["gsecret"]

    conn = await get_connection(db_session, await tenant_id_of(db_session, "cb-co"), "google")
    assert conn.status == "connected" and conn.account_email == "owner@acme.test"
    assert "g-access-1" not in conn.encrypted_access_token
    assert decrypt_secret(conn.encrypted_access_token) == "g-access-1"
    assert decrypt_secret(conn.encrypted_refresh_token) == "g-refresh-1"
    assert conn.expires_at > datetime.now(UTC) and conn.connected_by_user_id is not None

    listing = {p["provider"]: p for p in (await client.get("/api/v1/oauth/providers", headers=_auth(tokens))).json()}
    google = listing["google"]
    assert google["connected"] and google["account_email"] == "owner@acme.test"
    assert google["status"] == "connected" and google["needs_reconnect"] is False
    assert "access_token" not in str(google)


async def test_callback_rejects_replay_expiry_and_garbage(
    client: AsyncClient, creds, providers: MockProviders
) -> None:
    tokens = await _signup(client, slug="rp-co", email="o@rpco.com")
    url = await authorize(client, tokens, "google")
    state = state_of(url)
    google_mocks(providers)
    assert redirect_query(await callback(client, "google", code="c", state=state))["connected"] == "google"
    replay = redirect_query(await callback(client, "google", code="c", state=state))
    assert replay["error"] == "state_replayed" and "connected" not in replay

    expired = st.sign_state(
        provider="google", purpose="connect", nonce="zzz", tenant_id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()), return_url="/integrations/hub",
        now=datetime.now(UTC) - timedelta(hours=1),
    )
    assert redirect_query(await callback(client, "google", code="c", state=expired))["error"] == "state_expired"
    assert redirect_query(await callback(client, "google", code="c", state="garbage"))["error"] == "invalid_state"
    assert redirect_query(await callback(client, "google", code="c"))["error"] == "invalid_state"
    # a validly signed state whose server-side row does not exist is also refused
    ghost = st.sign_state(
        provider="google", purpose="connect", nonce="ghost", tenant_id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()), return_url="/integrations/hub",
    )
    assert redirect_query(await callback(client, "google", code="c", state=ghost))["error"] == "invalid_state"


async def test_callback_error_paths(client: AsyncClient, creds, providers: MockProviders, db_session) -> None:
    tokens = await _signup(client, slug="er-co", email="o@erco.com")
    # user denies consent
    url = await authorize(client, tokens, "google", return_url="/integrations/hub?tab=cal")
    denied = await callback(client, "google", error="access_denied", state=state_of(url))
    assert denied.headers["location"].startswith(f"{FRONT}/integrations/hub?tab=cal&error=access_denied")
    # provider rejects the code
    providers.add("POST", "https://oauth2.googleapis.com/token", {"error": "invalid_grant"}, status=400)
    url = await authorize(client, tokens, "google")
    failed = redirect_query(await callback(client, "google", code="bad", state=state_of(url)))
    assert failed["error"] == "token_exchange_failed"
    # missing code
    url = await authorize(client, tokens, "google")
    assert redirect_query(await callback(client, "google", state=state_of(url)))["error"] == "invalid_callback"
    assert await get_connection(db_session, await tenant_id_of(db_session, "er-co"), "google") is None
    # unknown provider never 500s
    assert redirect_query(await callback(client, "nope", code="c", state="s"))["error"] == "unknown_provider"


async def test_state_signed_for_one_provider_cannot_complete_another(
    client: AsyncClient, creds, providers: MockProviders
) -> None:
    tokens = await _signup(client, slug="xp-co", email="o@xpco.com")
    url = await authorize(client, tokens, "google")
    out = redirect_query(await callback(client, "hubspot", code="c", state=state_of(url)))
    assert out["error"] == "invalid_state"


# ---------------------------------------------------------------- tenant isolation


async def test_connections_are_tenant_isolated(
    client: AsyncClient, creds, providers: MockProviders, db_session: AsyncSession
) -> None:
    a = await _signup(client, slug="iso-a", email="o@isoa.com")
    b = await _signup(client, slug="iso-b", email="o@isob.com")
    assert (await connect_google(client, a, providers)).status_code == 302

    a_list = {p["provider"]: p for p in (await client.get("/api/v1/oauth/providers", headers=_auth(a))).json()}
    b_list = {p["provider"]: p for p in (await client.get("/api/v1/oauth/providers", headers=_auth(b))).json()}
    assert a_list["google"]["connected"] is True and b_list["google"]["connected"] is False

    assert (await client.delete("/api/v1/oauth/google", headers=_auth(b))).status_code == 404
    assert (await client.get("/api/v1/oauth/google/resources", headers=_auth(b))).status_code == 404
    b_id = await tenant_id_of(db_session, "iso-b")
    with pytest.raises(OAuthError) as exc:
        await get_valid_access_token(db_session, b_id, "google")
    assert exc.value.code == "not_connected"
    a_id = await tenant_id_of(db_session, "iso-a")
    assert await get_valid_access_token(db_session, a_id, "google") == "g-access-1"
    # A is still connected after B's attempted delete
    assert await get_connection(db_session, a_id, "google") is not None


async def test_state_tenant_comes_from_signed_state_not_request(
    client: AsyncClient, creds, providers: MockProviders, db_session: AsyncSession
) -> None:
    a = await _signup(client, slug="sg-a", email="o@sga.com")
    await _signup(client, slug="sg-b", email="o@sgb.com")
    google_mocks(providers)
    url = await authorize(client, a, "google")
    # extra query params (tenant_id etc.) in the callback are ignored
    await callback(client, "google", code="c", state=state_of(url), tenant_id=str(uuid.uuid4()))
    assert await get_connection(db_session, await tenant_id_of(db_session, "sg-a"), "google")
    assert await get_connection(db_session, await tenant_id_of(db_session, "sg-b"), "google") is None


# ---------------------------------------------------------------- token manager


async def test_valid_token_not_refreshed_until_near_expiry(
    db_session: AsyncSession, providers: MockProviders, creds
) -> None:
    tid = uuid.uuid4()
    db_session.add(Tenant(id=tid, name="T", slug=f"t-{tid.hex[:8]}"))
    await db_session.commit()
    await seed_connection(db_session, tid, expires_in=3600)
    assert await get_valid_access_token(db_session, tid, "google") == "a-1"
    assert providers.calls == []


async def _expired_connection(db: AsyncSession, provider: str = "google", **kw) -> uuid.UUID:
    tid = uuid.uuid4()
    db.add(Tenant(id=tid, name="T", slug=f"t-{tid.hex[:8]}"))
    await db.commit()
    conn = await seed_connection(db, tid, provider, **kw)
    conn.expires_at = datetime.now(UTC) - timedelta(minutes=5)
    await db.commit()
    return tid


async def test_refresh_persists_new_token(
    db_session: AsyncSession, providers: MockProviders, creds
) -> None:
    tid = await _expired_connection(db_session)
    providers.add(
        "POST", "https://oauth2.googleapis.com/token",
        {"access_token": "a-2", "expires_in": 3600},
    )
    assert await get_valid_access_token(db_session, tid, "google") == "a-2"
    body = parse_qs(providers.calls[0].content.decode())
    assert body["grant_type"] == ["refresh_token"] and body["refresh_token"] == ["r-1"]
    await db_session.rollback()
    conn = await get_connection(db_session, tid, "google")
    await db_session.refresh(conn)
    assert decrypt_secret(conn.encrypted_access_token) == "a-2"
    # provider did not rotate the refresh token: the old one is kept
    assert decrypt_secret(conn.encrypted_refresh_token) == "r-1"
    assert conn.status == "connected" and conn.expires_at > datetime.now(UTC)
    # now fresh: no second refresh call
    assert await get_valid_access_token(db_session, tid, "google") == "a-2"
    assert len(providers.calls) == 1


async def test_refresh_stores_rotated_refresh_token(
    db_session: AsyncSession, providers: MockProviders, creds
) -> None:
    tid = await _expired_connection(db_session, "microsoft")
    providers.add(
        "POST", "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        {"access_token": "ms-2", "refresh_token": "ms-r-2", "expires_in": 3600},
    )
    assert await get_valid_access_token(db_session, tid, "microsoft") == "ms-2"
    conn = await get_connection(db_session, tid, "microsoft")
    await db_session.refresh(conn)
    assert decrypt_secret(conn.encrypted_refresh_token) == "ms-r-2"


async def test_refresh_rejected_marks_expired_and_blocks_further_calls(
    db_session: AsyncSession, providers: MockProviders, creds
) -> None:
    tid = await _expired_connection(db_session)
    providers.add(
        "POST", "https://oauth2.googleapis.com/token",
        {"error": "invalid_grant", "error_description": "Token has been revoked"}, status=400,
    )
    with pytest.raises(OAuthError) as exc:
        await get_valid_access_token(db_session, tid, "google")
    assert exc.value.code == "reconnect_required"
    conn = await get_connection(db_session, tid, "google")
    await db_session.refresh(conn)
    assert conn.status == "expired" and "revoked" in conn.last_error
    # no HTTP on the next attempt: the status gates it
    calls = len(providers.calls)
    with pytest.raises(OAuthError) as exc:
        await get_valid_access_token(db_session, tid, "google")
    assert exc.value.code == "reconnect_required" and len(providers.calls) == calls


async def test_transient_refresh_failure_marks_error_then_recovers(
    db_session: AsyncSession, providers: MockProviders, creds
) -> None:
    tid = await _expired_connection(db_session)
    providers.add("POST", "https://oauth2.googleapis.com/token", {"error": "boom"}, status=503)
    with pytest.raises(OAuthError) as exc:
        await get_valid_access_token(db_session, tid, "google")
    assert exc.value.code == "refresh_failed"
    conn = await get_connection(db_session, tid, "google")
    await db_session.refresh(conn)
    assert conn.status == "error" and conn.last_error
    # provider recovers; the connection is usable again and status is cleared
    providers.routes.clear()
    providers.add("POST", "https://oauth2.googleapis.com/token", {"access_token": "a-3", "expires_in": 60})
    assert await get_valid_access_token(db_session, tid, "google") == "a-3"
    await db_session.refresh(conn)
    assert conn.status == "connected" and conn.last_error is None


async def test_expired_token_without_refresh_token_needs_reconnect(
    db_session: AsyncSession, providers: MockProviders, creds
) -> None:
    tid = await _expired_connection(db_session, refresh=None)
    with pytest.raises(OAuthError) as exc:
        await get_valid_access_token(db_session, tid, "google")
    assert exc.value.code == "reconnect_required" and providers.calls == []
    conn = await get_connection(db_session, tid, "google")
    await db_session.refresh(conn)
    assert conn.status == "expired"


async def test_concurrent_refresh_is_serialised_by_row_lock(
    db_session: AsyncSession, providers: MockProviders, creds
) -> None:
    from packages.database.session import session_scope

    tid = await _expired_connection(db_session)
    providers.add("POST", "https://oauth2.googleapis.com/token", {"access_token": "a-9", "expires_in": 3600})

    async def one() -> str:
        async with session_scope() as s:
            return await get_valid_access_token(s, tid, "google")

    results = await asyncio.gather(one(), one(), one())
    assert results == ["a-9", "a-9", "a-9"]
    assert len(providers.calls_to("POST", "https://oauth2.googleapis.com/token")) == 1


async def test_reconnect_keeps_refresh_token_when_provider_omits_it(
    db_session: AsyncSession,
) -> None:
    tid = uuid.uuid4()
    db_session.add(Tenant(id=tid, name="T", slug=f"t-{tid.hex[:8]}"))
    await db_session.commit()
    await seed_connection(db_session, tid, refresh="keep-me", extra={"selected": {"calendar_id": "c1"}})
    conn = await upsert_connection(
        db_session, tenant_id=tid, user_id=None, provider="google",
        token=TokenResult(access_token="a-new", refresh_token=None, expires_in=10),
        identity=Identity(label="owner@acme.test", external_id="x1"), requested_scopes=[],
    )
    assert decrypt_secret(conn.encrypted_refresh_token) == "keep-me"
    assert conn.extra["selected"] == {"calendar_id": "c1"}  # same account keeps its picks
    other = await upsert_connection(
        db_session, tenant_id=tid, user_id=None, provider="google",
        token=TokenResult(access_token="a-x", expires_in=10),
        identity=Identity(label="someone@else.test", external_id="DIFFERENT"), requested_scopes=[],
    )
    assert other.encrypted_refresh_token is None and "selected" not in other.extra


# ---------------------------------------------------------------- disconnect


async def test_disconnect_revokes_at_provider_and_deletes(
    client: AsyncClient, creds, providers: MockProviders, db_session: AsyncSession
) -> None:
    tokens = await _signup(client, slug="dc-co", email="o@dcco.com")
    await connect_google(client, tokens, providers)
    providers.add("POST", "https://oauth2.googleapis.com/revoke", {}, status=200)
    resp = await client.delete("/api/v1/oauth/google", headers=_auth(tokens))
    assert resp.status_code == 204
    revoke = providers.calls_to("POST", "https://oauth2.googleapis.com/revoke")
    assert parse_qs(revoke[0].content.decode())["token"] == ["g-refresh-1"]
    assert await get_connection(db_session, await tenant_id_of(db_session, "dc-co"), "google") is None


async def test_disconnect_still_deletes_when_revoke_fails(
    client: AsyncClient, creds, providers: MockProviders, db_session: AsyncSession
) -> None:
    tokens = await _signup(client, slug="dc2-co", email="o@dc2co.com")
    await connect_google(client, tokens, providers)
    providers.add("POST", "https://oauth2.googleapis.com/revoke", {"error": "x"}, status=500)
    assert (await client.delete("/api/v1/oauth/google", headers=_auth(tokens))).status_code == 204
    assert await get_connection(db_session, await tenant_id_of(db_session, "dc2-co"), "google") is None


# ---------------------------------------------------------------- provider specifics


async def test_zoho_is_data_centre_aware(
    client: AsyncClient, creds, providers: MockProviders, db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(creds, "zoho_accounts_domain", "https://accounts.zoho.in")
    tokens = await _signup(client, slug="zoho-co", email="o@zohoco.com")
    url = await authorize(client, tokens, "zoho")
    assert url.startswith("https://accounts.zoho.in/oauth/v2/auth?")
    q = parse_qs(urlparse(url).query)
    assert q["access_type"] == ["offline"] and "ZohoCRM.modules.ALL" in q["scope"][0]
    # the user turns out to live in the .eu data centre: Zoho says so on the callback
    providers.add(
        "POST", "https://accounts.zoho.eu/oauth/v2/token",
        {"access_token": "z-1", "refresh_token": "z-r", "expires_in": 3600,
         "api_domain": "https://www.zohoapis.eu"},
    )
    providers.add("GET", "https://accounts.zoho.eu/oauth/user/info", {"Email": "z@acme.test", "ZUID": "9"})
    out = await callback(
        client, "zoho", code="c", state=state_of(url), **{"accounts-server": "https://accounts.zoho.eu"}
    )
    assert redirect_query(out)["connected"] == "zoho"
    conn = await get_connection(db_session, await tenant_id_of(db_session, "zoho-co"), "zoho")
    assert conn.extra["api_domain"] == "https://www.zohoapis.eu"
    assert conn.extra["accounts_server"] == "https://accounts.zoho.eu"
    # an attacker-controlled accounts-server is refused
    url = await authorize(client, tokens, "zoho")
    evil = await callback(
        client, "zoho", code="c", state=state_of(url), **{"accounts-server": "https://evil.test"}
    )
    assert redirect_query(evil)["error"] == "invalid_zoho_domain"


async def test_salesforce_uses_pkce_and_stores_instance_url(
    client: AsyncClient, creds, providers: MockProviders, db_session: AsyncSession
) -> None:
    tokens = await _signup(client, slug="sf-co", email="o@sfco.com")
    url = await authorize(client, tokens, "salesforce")
    assert url.startswith("https://login.salesforce.com/services/oauth2/authorize?")
    assert "code_challenge" in url
    providers.add(
        "POST", "https://login.salesforce.com/services/oauth2/token",
        {"access_token": "sf-1", "refresh_token": "sf-r", "instance_url": "https://acme.my.salesforce.com",
         "id": "https://login.salesforce.com/id/00D1/0051"},
    )
    providers.add(
        "GET", "https://login.salesforce.com/id/00D1/0051",
        {"email": "sf@acme.test", "user_id": "0051", "organization_id": "00D1"},
    )
    assert redirect_query(await callback(client, "salesforce", code="c", state=state_of(url)))["connected"] == "salesforce"
    conn = await get_connection(db_session, await tenant_id_of(db_session, "sf-co"), "salesforce")
    assert conn.extra["instance_url"] == "https://acme.my.salesforce.com"
    assert conn.account_email == "sf@acme.test"
    # a hostile instance_url is refused
    providers.routes.clear()
    providers.add(
        "POST", "https://login.salesforce.com/services/oauth2/token",
        {"access_token": "x", "instance_url": "https://evil.test"},
    )
    url = await authorize(client, tokens, "salesforce")
    assert redirect_query(await callback(client, "salesforce", code="c", state=state_of(url)))["error"] == "token_exchange_failed"


async def test_hubspot_connect_and_refresh_endpoint_shape(
    client: AsyncClient, creds, providers: MockProviders, db_session: AsyncSession
) -> None:
    tokens = await _signup(client, slug="hs-co", email="o@hsco.com")
    url = await authorize(client, tokens, "hubspot")
    assert "crm.objects.contacts.write" in url and "code_challenge" not in url
    providers.add(
        "POST", "https://api.hubapi.com/oauth/v1/token",
        {"access_token": "hs-1", "refresh_token": "hs-r", "expires_in": 1800},
    )
    providers.add(
        "GET", "https://api.hubapi.com/oauth/v1/access-tokens/hs-1",
        {"user": "me@acme.test", "hub_id": 42, "hub_domain": "acme.hubspot.test"},
    )
    assert redirect_query(await callback(client, "hubspot", code="c", state=state_of(url)))["connected"] == "hubspot"
    conn = await get_connection(db_session, await tenant_id_of(db_session, "hs-co"), "hubspot")
    assert conn.external_account_id == "42" and conn.account_label == "acme.hubspot.test"


def _shopify_hmac(params: dict[str, str], secret: str = "shsecret") -> str:
    msg = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


async def test_shopify_requires_valid_shop_and_verifies_hmac(
    client: AsyncClient, creds, providers: MockProviders, db_session: AsyncSession
) -> None:
    tokens = await _signup(client, slug="shop-co", email="o@shopco.com")
    bad = await client.post(
        "/api/v1/oauth/shopify/authorize", headers=_auth(tokens), json={"shop": "evil.com/x"}
    )
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == "invalid_shop"
    missing = await client.post("/api/v1/oauth/shopify/authorize", headers=_auth(tokens), json={})
    assert missing.status_code == 400

    url = await authorize(client, tokens, "shopify", shop="My-Store")
    assert url.startswith("https://my-store.myshopify.com/admin/oauth/authorize?")
    providers.add(
        "POST", "https://my-store.myshopify.com/admin/oauth/access_token",
        {"access_token": "shpat_1", "scope": "read_orders,read_customers"},
    )
    providers.add(
        "GET", "https://my-store.myshopify.com/admin/api/2024-10/shop.json",
        {"shop": {"name": "My Store", "email": "shop@acme.test"}},
    )

    def signed(**extra: str) -> dict[str, str]:
        params = {"code": "c", "shop": "my-store.myshopify.com", "timestamp": "1700000000", **extra}
        return {**params, "hmac": _shopify_hmac(params)}

    # forged HMAC is rejected and nothing is stored
    forged = signed(state=state_of(url))
    forged["hmac"] = "0" * 64
    assert redirect_query(await callback(client, "shopify", **forged))["error"] == "invalid_hmac"
    assert providers.calls_to("POST", "https://my-store.myshopify.com/admin/oauth/access_token") == []

    url = await authorize(client, tokens, "shopify", shop="my-store")
    ok = await callback(client, "shopify", **signed(state=state_of(url)))
    assert redirect_query(ok)["connected"] == "shopify"
    tid = await tenant_id_of(db_session, "shop-co")
    conn = await get_connection(db_session, tid, "shopify")
    assert conn.extra["shop"] == "my-store.myshopify.com" and conn.external_account_id == "my-store.myshopify.com"
    assert conn.expires_at is None  # offline token never expires
    # mirrored into the legacy connector so the lookup_order tool keeps working
    connector = (
        await db_session.execute(
            select(IntegrationConnector).where(
                IntegrationConnector.tenant_id == tid, IntegrationConnector.type == "shopify"
            )
        )
    ).scalar_one()
    from app.services.platform_connectors import decrypt_config

    assert decrypt_config(connector.config) == {
        "shop_domain": "my-store.myshopify.com", "access_token": "shpat_1"
    }
    # a callback for a DIFFERENT shop than the one in the signed state is refused
    url = await authorize(client, tokens, "shopify", shop="my-store")
    params = {"code": "c", "shop": "other-store.myshopify.com", "timestamp": "1", "state": state_of(url)}
    params["hmac"] = _shopify_hmac(params)
    assert redirect_query(await callback(client, "shopify", **params))["error"] == "invalid_state"

    providers.add("DELETE", "https://my-store.myshopify.com/admin/api_permissions/current.json", {}, status=200)
    assert (await client.delete("/api/v1/oauth/shopify", headers=_auth(tokens))).status_code == 204
    db_session.expire_all()
    remaining = (
        await db_session.execute(
            select(IntegrationConnector).where(IntegrationConnector.tenant_id == tid)
        )
    ).scalars().all()
    assert remaining == []


async def test_slack_connect_pick_channel_and_events(
    client: AsyncClient, creds, providers: MockProviders, db_session: AsyncSession
) -> None:
    tokens = await _signup(client, slug="sl-co", email="o@slco.com")
    url = await authorize(client, tokens, "slack")
    q = parse_qs(urlparse(url).query)
    assert q["scope"] == ["chat:write,chat:write.public,channels:read,groups:read"]
    providers.add(
        "POST", "https://slack.com/api/oauth.v2.access",
        {"ok": True, "access_token": "xoxb-1", "team": {"id": "T1", "name": "Acme HQ"},
         "bot_user_id": "B1", "authed_user": {"id": "U1"}},
    )
    assert redirect_query(await callback(client, "slack", code="c", state=state_of(url)))["connected"] == "slack"
    conn = await get_connection(db_session, await tenant_id_of(db_session, "sl-co"), "slack")
    assert conn.account_label == "Acme HQ" and conn.expires_at is None
    assert decrypt_secret(conn.encrypted_access_token) == "xoxb-1"

    providers.add(
        "GET", "https://slack.com/api/conversations.list",
        {"ok": True, "channels": [{"id": "C1", "name": "support"}, {"id": "C2", "name": "ops", "is_private": True}]},
    )
    res = await client.get("/api/v1/oauth/slack/resources", headers=_auth(tokens))
    assert res.status_code == 200 and res.json()["kind"] == "channels"
    assert [i["id"] for i in res.json()["items"]] == ["C1", "C2"]
    sel = await client.post(
        "/api/v1/oauth/slack/select", headers=_auth(tokens),
        json={"resource_id": "C1", "events": ["ticket.created"]},
    )
    assert sel.json()["selected"] == {"channel_id": "C1", "channel_name": "#support", "events": ["ticket.created"]}
    nope = await client.post("/api/v1/oauth/slack/select", headers=_auth(tokens), json={"resource_id": "C9"})
    assert nope.status_code == 400 and nope.json()["detail"]["code"] == "resource_not_found"


# ---------------------------------------------------------------- Sign in with Google / Microsoft


def _id_token(provider: str = "google", **overrides) -> str:
    base = {
        "iss": "https://accounts.google.com",
        "aud": "gid",
        "exp": int(time.time()) + 600,
        "email": "owner@logintest.com",
        "email_verified": True,
        "sub": "g-1",
    }
    if provider == "microsoft":
        tid = "11111111-1111-1111-1111-111111111111"
        base = {
            "iss": f"https://login.microsoftonline.com/{tid}/v2.0", "tid": tid, "aud": "mid",
            "exp": int(time.time()) + 600, "email": "owner@logintest.com", "xms_edov": True,
        }
    base.update(overrides)
    return jwt.encode(base, "unverified", algorithm="HS256")


async def _login_start(client: AsyncClient, provider: str = "google", **params) -> tuple[str, str]:
    resp = await client.get(f"/api/v1/auth/oauth/{provider}/login", params=params, follow_redirects=False)
    assert resp.status_code == 302, resp.text
    q = parse_qs(urlparse(resp.headers["location"]).query)
    return q["state"][0], q["nonce"][0]


def _login_token_mock(mock: MockProviders, provider: str, nonce: str, **claims) -> None:
    url = (
        "https://oauth2.googleapis.com/token"
        if provider == "google"
        else "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    )
    mock.add(
        "POST", url,
        {"access_token": "irrelevant", "id_token": _id_token(provider, nonce=nonce, **claims), "expires_in": 3600},
    )


async def test_login_with_google_issues_normal_jwt_for_existing_user(
    client: AsyncClient, creds, providers: MockProviders
) -> None:
    tokens = await _signup(client, slug="lg-co", email="owner@logintest.com")
    resp = await client.get("/api/v1/auth/oauth/google/login", follow_redirects=False)
    loc = resp.headers["location"]
    q = parse_qs(urlparse(loc).query)
    assert loc.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert q["redirect_uri"] == [f"{PUBLIC}/api/v1/auth/oauth/google/callback"]
    assert q["scope"] == ["openid email profile"] and "access_type" not in q
    state, nonce = q["state"][0], q["nonce"][0]
    _login_token_mock(providers, "google", nonce)
    out = await client.get(
        "/api/v1/auth/oauth/google/callback", params={"code": "c", "state": state}, follow_redirects=False
    )
    location = out.headers["location"]
    assert location.startswith(f"{FRONT}/sso/callback#access_token=")
    frag = parse_qs(urlparse(location).fragment)
    claims = jwt.decode(frag["access_token"][0], options={"verify_signature": False})
    original = jwt.decode(tokens["access_token"], options={"verify_signature": False})
    assert claims["sub"] == original["sub"] and claims["tenant_id"] == original["tenant_id"]
    me = await client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {frag['access_token'][0]}"})
    assert me.status_code == 200 and me.json()["email"] == "owner@logintest.com"
    # replaying the same callback fails
    again = await client.get(
        "/api/v1/auth/oauth/google/callback", params={"code": "c", "state": state}, follow_redirects=False
    )
    assert parse_qs(urlparse(again.headers["location"]).query)["oauth_error"] == ["state_replayed"]


async def test_login_never_creates_users_or_tenants(
    client: AsyncClient, creds, providers: MockProviders, db_session: AsyncSession
) -> None:
    from packages.database.models import User

    state, nonce = await _login_start(client)
    _login_token_mock(providers, "google", nonce, email="stranger@nowhere.test")
    out = await client.get(
        "/api/v1/auth/oauth/google/callback", params={"code": "c", "state": state}, follow_redirects=False
    )
    q = parse_qs(urlparse(out.headers["location"]).query)
    assert out.headers["location"].startswith(f"{FRONT}/login?")
    assert q["oauth_error"] == ["no_account"] and "invite" in q["sso_error"][0].lower()
    assert (await db_session.execute(select(User))).scalars().all() == []
    assert (await db_session.execute(select(Tenant))).scalars().all() == []


async def test_login_rejects_unverified_email_bad_nonce_and_wrong_audience(
    client: AsyncClient, creds, providers: MockProviders
) -> None:
    await _signup(client, slug="lg2-co", email="owner@logintest.com")

    async def attempt(**claims) -> str:
        state, nonce = await _login_start(client)
        providers.routes.clear()
        _login_token_mock(providers, "google", claims.pop("nonce", nonce), **claims)
        out = await client.get(
            "/api/v1/auth/oauth/google/callback", params={"code": "c", "state": state}, follow_redirects=False
        )
        return parse_qs(urlparse(out.headers["location"]).query).get("oauth_error", ["ok"])[0]

    assert await attempt(email_verified=False) == "email_not_verified"
    assert await attempt(nonce="attacker-nonce") == "invalid_id_token"
    assert await attempt(aud="someone-elses-app") == "invalid_id_token"
    assert await attempt(exp=int(time.time()) - 3600) == "invalid_id_token"
    assert await attempt(iss="https://evil.test") == "invalid_id_token"
    assert await attempt() == "ok"


async def test_microsoft_is_not_offered_for_sign_in_even_when_it_is_configured(
    client: AsyncClient, creds, db_session: AsyncSession
) -> None:
    assert (await client.get("/api/v1/auth/oauth/providers")).json() == {"providers": ["google"]}
    for params in ({}, {"mode": "signup", "company_name": "Contoso"}):
        out = await client.get("/api/v1/auth/oauth/microsoft/login", params=params, follow_redirects=False)
        assert out.status_code == 302 and out.headers["location"].startswith(f"{FRONT}/login?")
        assert parse_qs(urlparse(out.headers["location"]).query)["oauth_error"] == ["unknown_provider"]
    assert (await db_session.execute(select(Tenant))).scalars().all() == []


async def test_login_multiple_workspaces_needs_slug_and_unconfigured_provider(
    client: AsyncClient, creds, providers: MockProviders, db_session: AsyncSession, monkeypatch
) -> None:
    from packages.database.models import MembershipStatus, Role, TenantUser, User

    await _signup(client, slug="mw-a", email="owner@logintest.com")
    await _signup(client, slug="mw-b", email="someone@else.com")
    user = (await db_session.execute(select(User).where(User.email == "owner@logintest.com"))).scalar_one()
    b_id = await tenant_id_of(db_session, "mw-b")
    role = (await db_session.execute(select(Role).where(Role.tenant_id.is_(None), Role.name == "OWNER"))).scalar_one()
    db_session.add(TenantUser(tenant_id=b_id, user_id=user.id, role_id=role.id, status=MembershipStatus.ACTIVE))
    await db_session.commit()

    state, nonce = await _login_start(client)
    _login_token_mock(providers, "google", nonce)
    out = await client.get(
        "/api/v1/auth/oauth/google/callback", params={"code": "c", "state": state}, follow_redirects=False
    )
    assert parse_qs(urlparse(out.headers["location"]).query)["oauth_error"] == ["tenant_selection_required"]

    state, nonce = await _login_start(client, tenant_slug="mw-b")
    providers.routes.clear()
    _login_token_mock(providers, "google", nonce)
    out = await client.get(
        "/api/v1/auth/oauth/google/callback", params={"code": "c", "state": state}, follow_redirects=False
    )
    frag = parse_qs(urlparse(out.headers["location"]).fragment)
    assert jwt.decode(frag["access_token"][0], options={"verify_signature": False})["tenant_id"] == str(b_id)

    monkeypatch.setattr(creds, "google_client_id", "")
    off = await client.get("/api/v1/auth/oauth/google/login", follow_redirects=False)
    assert parse_qs(urlparse(off.headers["location"]).query)["oauth_error"] == ["provider_not_configured"]
    assert (await client.get("/api/v1/auth/oauth/providers")).json() == {"providers": []}
    assert (await client.get("/api/v1/auth/oauth/hubspot/login", follow_redirects=False)).status_code == 302


async def test_empty_env_placeholders_fall_back_to_defaults(
    client: AsyncClient, creds, monkeypatch
) -> None:
    """.env ships EMPTY placeholders; empty must mean 'use the default'."""
    monkeypatch.setattr(creds, "zoho_accounts_domain", "")
    monkeypatch.setattr(creds, "salesforce_login_url", "")
    monkeypatch.setattr(creds, "microsoft_tenant_id", "")
    tokens = await _signup(client, slug="ph-co", email="o@phco.com")
    assert (await authorize(client, tokens, "zoho")).startswith("https://accounts.zoho.com/oauth/v2/auth?")
    assert (await authorize(client, tokens, "salesforce")).startswith(
        "https://login.salesforce.com/services/oauth2/authorize?"
    )
    assert (await authorize(client, tokens, "microsoft")).startswith(
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?"
    )


# ---------------------------------------------------------------- Sign up with Google / Microsoft


async def _finish_login(client: AsyncClient, providers: MockProviders, provider: str = "google", start_params=None, **claims):
    state, nonce = await _login_start(client, provider, **(start_params or {}))
    providers.routes.clear()  # each sign-in has its own nonce, so only the latest token answer may match
    _login_token_mock(providers, provider, nonce, **claims)
    out = await client.get(
        f"/api/v1/auth/oauth/{provider}/callback", params={"code": "c", "state": state}, follow_redirects=False
    )
    return out.headers["location"]


async def test_google_signup_creates_a_workspace_on_the_signup_plan_for_a_new_verified_email(
    client: AsyncClient, creds, providers: MockProviders, db_session: AsyncSession
) -> None:
    from packages.database.models import AuditLog, TenantUser, User

    location = await _finish_login(
        client, providers, start_params={"mode": "signup", "company_name": "Lotus Salon"},
        email="meera@lotus.example.com", name="Meera Rao",
    )
    assert location.startswith(f"{FRONT}/sso/callback#access_token=")
    frag = parse_qs(urlparse(location).fragment)
    me = await client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {frag['access_token'][0]}"})
    assert me.status_code == 200 and me.json()["email"] == "meera@lotus.example.com" and me.json()["full_name"] == "Meera Rao"
    tenant = (await db_session.execute(select(Tenant))).scalar_one()
    assert tenant.name == "Lotus Salon" and tenant.slug == "lotus-salon" and tenant.plan_tier == get_settings().signup_plan_tier
    user = (await db_session.execute(select(User))).scalar_one()
    assert user.email_verified_at is not None and user.hashed_password  # no usable password was chosen by anyone
    membership = (await db_session.execute(select(TenantUser))).scalar_one()
    assert membership.user_id == user.id and membership.tenant_id == tenant.id
    actions = [row.action for row in (await db_session.execute(select(AuditLog))).scalars().all()]
    assert "oauth.signup" in actions


async def test_google_signup_without_a_company_name_uses_their_name_and_never_collides(
    client: AsyncClient, creds, providers: MockProviders, db_session: AsyncSession
) -> None:
    await _finish_login(client, providers, start_params={"mode": "signup"}, email="a@one.example.com", name="Priya Shah")
    await _finish_login(client, providers, start_params={"mode": "signup"}, email="b@two.example.com", name="Priya Shah")
    tenants = (await db_session.execute(select(Tenant))).scalars().all()
    assert len(tenants) == 2 and {t.name for t in tenants} == {"Priya's workspace"}
    assert len({t.slug for t in tenants}) == 2 and any(t.slug == "priya-s-workspace" for t in tenants)


async def test_google_signup_for_an_email_that_already_has_an_account_just_signs_in(
    client: AsyncClient, creds, providers: MockProviders, db_session: AsyncSession
) -> None:
    from packages.database.models import User

    tokens = await _signup(client, slug="have-co", email="owner@logintest.com")
    location = await _finish_login(client, providers, start_params={"mode": "signup", "company_name": "Another"})
    frag = parse_qs(urlparse(location).fragment)
    claims = jwt.decode(frag["access_token"][0], options={"verify_signature": False})
    original = jwt.decode(tokens["access_token"], options={"verify_signature": False})
    assert claims["sub"] == original["sub"] and claims["tenant_id"] == original["tenant_id"]
    assert len((await db_session.execute(select(Tenant))).scalars().all()) == 1
    assert len((await db_session.execute(select(User))).scalars().all()) == 1


async def test_signup_never_runs_for_an_unverified_email_or_without_the_signup_mode(
    client: AsyncClient, creds, providers: MockProviders, db_session: AsyncSession
) -> None:
    from packages.database.models import User

    unverified = await _finish_login(client, providers, start_params={"mode": "signup"}, email="x@y.example.com", email_verified=False)
    assert parse_qs(urlparse(unverified).query)["oauth_error"] == ["email_not_verified"]
    plain = await _finish_login(client, providers, email="x@y.example.com")  # an ordinary sign-in
    assert parse_qs(urlparse(plain).query)["oauth_error"] == ["no_account"]
    assert (await db_session.execute(select(User))).scalars().all() == []
    assert (await db_session.execute(select(Tenant))).scalars().all() == []


async def test_signup_mode_inputs_are_validated(client: AsyncClient, creds) -> None:
    assert (await client.get("/api/v1/auth/oauth/google/login", params={"mode": "admin"}, follow_redirects=False)).status_code == 422
    too_long = await client.get("/api/v1/auth/oauth/google/login", params={"mode": "signup", "company_name": "x" * 200}, follow_redirects=False)
    assert too_long.status_code == 422
