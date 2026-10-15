"""OAuth 2.1 in front of /mcp: discovery, DCR, PKCE code flow, rotation, gate."""

import base64
import hashlib
import uuid
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from app.core.security import hash_password
from app.mcp import extended
from httpx import AsyncClient
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import select

from packages.database.models import MembershipStatus, Role, Tenant, TenantUser, User
from packages.database.session import session_scope

REDIRECT = "http://localhost:6274/callback"
PASSWORD = "supersecret1"


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(uuid.uuid4().bytes + uuid.uuid4().bytes).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


async def _signup(client: AsyncClient, slug="oauth-co", email="owner@oauth-co.com") -> None:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"company_name": "OAuth Co", "tenant_slug": slug, "full_name": "Owner", "email": email, "password": PASSWORD},
    )
    assert resp.status_code == 201, resp.text


async def _register(client: AsyncClient, redirect=REDIRECT) -> str:
    resp = await client.post("/oauth/register", json={"client_name": "Test AI", "redirect_uris": [redirect]})
    assert resp.status_code == 201, resp.text
    return resp.json()["client_id"]


def _params(client_id: str, challenge: str, **extra) -> dict:
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "xyz",
        **extra,
    }


async def _authorize(client: AsyncClient, params: dict, email="owner@oauth-co.com", grant=("calls:read",), **extra):
    data = {**params, "email": email, "password": PASSWORD, "action": "approve", "grant": list(grant), **extra}
    return await client.post("/oauth/authorize", data=data)


async def _tokens(client: AsyncClient, client_id: str, grant=("calls:read",)) -> dict:
    verifier, challenge = _pkce()
    resp = await _authorize(client, _params(client_id, challenge), grant=grant)
    assert resp.status_code == 303, resp.text
    query = parse_qs(urlparse(resp.headers["location"]).query)
    assert query["state"] == ["xyz"] and "iss" in query
    token = await client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": query["code"][0],
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200, token.text
    return token.json()


async def test_discovery_documents(client: AsyncClient):
    prm = (await client.get("/.well-known/oauth-protected-resource")).json()
    assert prm["resource"].endswith("/mcp") and prm["authorization_servers"]
    meta = (await client.get("/.well-known/oauth-authorization-server")).json()
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["registration_endpoint"].endswith("/oauth/register")
    assert (await client.get("/.well-known/oauth-protected-resource/mcp")).status_code == 200


async def test_mcp_endpoint_challenges_unauthenticated_clients(client: AsyncClient):
    resp = await client.post("/mcp/", json={})
    assert resp.status_code == 401
    assert "resource_metadata=" in resp.headers["www-authenticate"]
    bad = await client.post("/mcp/", json={}, headers={"Authorization": "Bearer ccb_oat_nope"})
    assert bad.status_code == 401 and 'error="invalid_token"' in bad.headers["www-authenticate"]


async def test_registration_rules(client: AsyncClient):
    for uris in ([], ["javascript:alert(1)"], ["http://evil.example/cb"], ["https://ok.example/cb#frag"]):
        resp = await client.post("/oauth/register", json={"redirect_uris": uris})
        assert resp.status_code == 400, uris
    ok = await client.post("/oauth/register", json={"redirect_uris": ["https://ok.example/cb", "cursor://cb"]})
    assert ok.status_code == 201
    secret = await client.post(
        "/oauth/register", json={"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "client_secret_post"}
    )
    assert secret.status_code == 400


async def test_full_flow_then_mcp_tool_call_and_scopes(client: AsyncClient):
    await _signup(client)
    client_id = await _register(client)
    tokens = await _tokens(client, client_id, grant=("calls:read",))
    assert tokens["scope"] == "calls:read" and tokens["token_type"] == "Bearer"

    ctx = SimpleNamespace(headers={"authorization": f"Bearer {tokens['access_token']}"})
    assert (await extended.list_calls(ctx))["calls"] == []
    with pytest.raises(ToolError, match="customers:write"):
        await extended.create_customer(ctx, name="X", phone="+15550001111")

    # The HTTP gate lets a valid OAuth token through to the MCP app itself; the
    # app's session manager only runs under the real lifespan, hence this error.
    with pytest.raises(RuntimeError, match="Task group"):
        await client.post("/mcp/", json={}, headers={"Authorization": f"Bearer {tokens['access_token']}"})


async def test_authorize_page_and_bad_redirects(client: AsyncClient):
    client_id = await _register(client)
    _, challenge = _pkce()
    page = await client.get("/oauth/authorize", params=_params(client_id, challenge))
    assert page.status_code == 200 and "Test AI" in page.text
    assert "default-src 'none'" in page.headers["content-security-policy"]
    # unregistered redirect: error page, never a redirect
    bad = await client.get("/oauth/authorize", params={**_params(client_id, challenge), "redirect_uri": "https://evil.example/"})
    assert bad.status_code == 400 and "location" not in bad.headers
    # missing PKCE: error goes back to the (registered) client
    nopkce = await client.get("/oauth/authorize", params={**_params(client_id, challenge), "code_challenge_method": "plain"})
    assert nopkce.status_code == 303 and "error=invalid_request" in nopkce.headers["location"]
    # xss in client name is escaped
    xss = await client.post("/oauth/register", json={"client_name": "<script>x</script>", "redirect_uris": [REDIRECT]})
    page = await client.get("/oauth/authorize", params=_params(xss.json()["client_id"], challenge))
    assert "<script>x" not in page.text


async def test_wrong_password_and_deny(client: AsyncClient):
    await _signup(client)
    client_id = await _register(client)
    _, challenge = _pkce()
    params = _params(client_id, challenge)
    bad = await client.post("/oauth/authorize", data={**params, "email": "owner@oauth-co.com", "password": "nope-nope-1", "action": "approve", "grant": ["calls:read"]})
    assert bad.status_code == 401
    deny = await client.post("/oauth/authorize", data={**params, "action": "deny"})
    assert deny.status_code == 303 and "error=access_denied" in deny.headers["location"]


async def test_code_is_single_use_and_pkce_enforced(client: AsyncClient):
    await _signup(client)
    client_id = await _register(client)
    verifier, challenge = _pkce()
    resp = await _authorize(client, _params(client_id, challenge))
    code = parse_qs(urlparse(resp.headers["location"]).query)["code"][0]
    body = {"grant_type": "authorization_code", "client_id": client_id, "code": code, "redirect_uri": REDIRECT}
    wrong = await client.post("/oauth/token", data={**body, "code_verifier": _pkce()[0]})
    assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_grant"
    ok = await client.post("/oauth/token", data={**body, "code_verifier": verifier})
    assert ok.status_code == 200
    again = await client.post("/oauth/token", data={**body, "code_verifier": verifier})
    assert again.status_code == 400
    other_client = await _register(client)
    stolen = await client.post("/oauth/token", data={**body, "client_id": other_client, "code_verifier": verifier})
    assert stolen.status_code == 400


async def test_refresh_rotation_and_replay_revokes_family(client: AsyncClient):
    await _signup(client)
    client_id = await _register(client)
    first = await _tokens(client, client_id)
    refreshed = await client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": first["refresh_token"]},
    )
    assert refreshed.status_code == 200
    second = refreshed.json()
    assert second["access_token"] != first["access_token"]
    # old pair is dead
    old = await client.post("/mcp/", json={}, headers={"Authorization": f"Bearer {first['access_token']}"})
    assert old.status_code == 401
    # replaying the rotated refresh token revokes the new pair too
    replay = await client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": first["refresh_token"]},
    )
    assert replay.status_code == 400
    dead = await client.post("/mcp/", json={}, headers={"Authorization": f"Bearer {second['access_token']}"})
    assert dead.status_code == 401


async def test_revoke_endpoint(client: AsyncClient):
    await _signup(client)
    client_id = await _register(client)
    tokens = await _tokens(client, client_id)
    assert (await client.post("/oauth/revoke", data={"token": tokens["access_token"]})).status_code == 200
    resp = await client.post("/mcp/", json={}, headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert resp.status_code == 401
    assert (await client.post("/oauth/revoke", data={"token": "garbage"})).status_code == 200


async def test_role_without_mcp_manage_cannot_authorize(client: AsyncClient):
    await _signup(client)
    async with session_scope() as db:
        tenant = (await db.execute(select(Tenant))).scalar_one()
        role = (await db.execute(select(Role).where(Role.name == "MANAGER", Role.tenant_id.is_(None)))).scalar_one()
        user = User(email="mgr@oauth-co.com", hashed_password=hash_password(PASSWORD), full_name="M")
        db.add(user)
        await db.flush()
        db.add(TenantUser(tenant_id=tenant.id, user_id=user.id, role_id=role.id, status=MembershipStatus.ACTIVE))
        await db.commit()
    client_id = await _register(client)
    _, challenge = _pkce()
    resp = await _authorize(client, _params(client_id, challenge), email="mgr@oauth-co.com")
    assert resp.status_code == 401 and "mcp.manage" in resp.text


async def test_ungrantable_scope_is_dropped(client: AsyncClient):
    await _signup(client)
    client_id = await _register(client)
    tokens = await _tokens(client, client_id, grant=("calls:read", "webhooks:manage", "bogus"))
    assert tokens["scope"] == "calls:read"
