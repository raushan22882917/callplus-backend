"""Sessions, per-key IP limits, key rotation, and audit filters/export."""

import csv
import io
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from app.core.ip_allowlist import invalidate_allowlist_cache
from app.core.security import create_access_token, create_refresh_token
from app.mcp.auth import McpAuthError, resolve_principal
from app.services import security_alerts, user_sessions
from httpx import AsyncClient
from sqlalchemy import select

from packages.database.models import AuditLog, Tenant, TenantUser, User
from packages.database.models.platform import TenantSecuritySettings
from packages.database.session import session_scope

PASSWORD = "supersecret1"
CHROME_MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
FIREFOX_WIN = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0"


async def _signup(client: AsyncClient, slug: str, ua: str = CHROME_MAC) -> dict:
    resp = await client.post(
        "/api/v1/auth/signup",
        headers={"User-Agent": ua},
        json={
            "company_name": slug,
            "tenant_slug": slug,
            "full_name": "Owner",
            "email": f"owner@{slug}.com",
            "password": PASSWORD,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _login(client: AsyncClient, slug: str, ua: str = CHROME_MAC) -> dict:
    resp = await client.post(
        "/api/v1/auth/login",
        headers={"User-Agent": ua},
        json={"email": f"owner@{slug}.com", "password": PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["tokens"]


def _h(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


@pytest.fixture
def emails(monkeypatch):
    sent: list[tuple[str, str]] = []

    async def fake(*, to_email, subject, html_body, headers=None):
        sent.append((to_email, subject))

    monkeypatch.setattr(user_sessions, "send_email", fake)
    monkeypatch.setattr(security_alerts, "send_email", fake)
    return sent


def test_describe_device():
    assert user_sessions.describe_device(CHROME_MAC) == "Chrome on macOS"
    assert user_sessions.describe_device(FIREFOX_WIN) == "Firefox on Windows"
    assert user_sessions.describe_device("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Safari/604.1") == "Safari on iPhone"
    assert user_sessions.describe_device(None) == "Unknown device"
    assert user_sessions.describe_device("curl/8.0") == "Unknown device"


async def test_each_sign_in_is_a_listed_session_and_marks_the_current_one(client: AsyncClient, emails):
    await _signup(client, "sess-co")
    first = await _login(client, "sess-co")
    second = await _login(client, "sess-co", FIREFOX_WIN)

    listed = (await client.get("/api/v1/auth/sessions", headers=_h(first))).json()
    assert len(listed) == 3  # signup + two logins
    assert sum(s["current"] for s in listed) == 1
    devices = sorted(s["device"] for s in listed)
    assert devices == ["Chrome on macOS", "Chrome on macOS", "Firefox on Windows"]
    as_second = (await client.get("/api/v1/auth/sessions", headers=_h(second))).json()
    assert [s["device"] for s in as_second if s["current"]] == ["Firefox on Windows"]


async def test_revoking_a_session_signs_that_device_out_at_once(client: AsyncClient, emails):
    await _signup(client, "revoke-co")
    mine = await _login(client, "revoke-co")
    other = await _login(client, "revoke-co", FIREFOX_WIN)
    other_id = next(
        s["id"] for s in (await client.get("/api/v1/auth/sessions", headers=_h(mine))).json() if s["device"].startswith("Firefox")
    )

    assert (await client.get("/api/v1/auth/sessions", headers=_h(other))).status_code == 200
    assert (await client.delete(f"/api/v1/auth/sessions/{other_id}", headers=_h(mine))).status_code == 204

    # Its access token is dead immediately, and its refresh token cannot get a new one.
    dead = await client.get("/api/v1/auth/sessions", headers=_h(other))
    assert dead.status_code == 401 and "signed out" in dead.json()["detail"]
    refresh = await client.post("/api/v1/auth/refresh", json={"refresh_token": other["refresh_token"]})
    assert refresh.status_code == 401
    # The device I'm on is unaffected, and revoking twice says "not found".
    assert (await client.get("/api/v1/auth/sessions", headers=_h(mine))).status_code == 200
    assert (await client.delete(f"/api/v1/auth/sessions/{other_id}", headers=_h(mine))).status_code == 404


async def test_you_cannot_see_or_revoke_someone_elses_sessions(client: AsyncClient, emails):
    await _signup(client, "alice-co")
    bob = await _signup(client, "bob-co")
    alice = await _login(client, "alice-co")
    alice_ids = [s["id"] for s in (await client.get("/api/v1/auth/sessions", headers=_h(alice))).json()]
    bob_view = (await client.get("/api/v1/auth/sessions", headers=_h(bob))).json()
    assert not {s["id"] for s in bob_view} & set(alice_ids)
    for sid in alice_ids:
        assert (await client.delete(f"/api/v1/auth/sessions/{sid}", headers=_h(bob))).status_code == 404
    assert (await client.get("/api/v1/auth/sessions", headers=_h(alice))).status_code == 200


async def test_sign_out_everywhere_else_keeps_this_browser(client: AsyncClient, emails):
    await _signup(client, "all-co")
    a = await _login(client, "all-co")
    b = await _login(client, "all-co", FIREFOX_WIN)
    revoked = await client.post("/api/v1/auth/sessions/revoke-others", headers=_h(a))
    assert revoked.status_code == 200 and revoked.json()["revoked"] == 2  # the signup session and b
    assert (await client.get("/api/v1/auth/sessions", headers=_h(b))).status_code == 401
    still = (await client.get("/api/v1/auth/sessions", headers=_h(a))).json()
    assert len(still) == 1 and still[0]["current"] is True


async def test_logout_ends_the_session_on_the_server(client: AsyncClient, emails):
    await _signup(client, "out-co")
    tokens = await _login(client, "out-co")
    assert (await client.post("/api/v1/auth/logout", headers=_h(tokens))).status_code == 204
    assert (await client.get("/api/v1/auth/sessions", headers=_h(tokens))).status_code == 401
    assert (await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})).status_code == 401


async def test_refresh_keeps_the_same_session(client: AsyncClient, emails):
    await _signup(client, "ref-co")
    tokens = await _login(client, "ref-co")
    before = (await client.get("/api/v1/auth/sessions", headers=_h(tokens))).json()
    refreshed = (await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})).json()
    after = (await client.get("/api/v1/auth/sessions", headers=_h(refreshed))).json()
    assert {s["id"] for s in before} == {s["id"] for s in after}
    assert [s["id"] for s in after if s["current"]] == [s["id"] for s in before if s["current"]]


async def test_tokens_from_before_sessions_existed_still_work_and_get_a_session_on_refresh(
    client: AsyncClient, emails
):
    await _signup(client, "old-co")
    async with session_scope() as db:
        membership = (await db.execute(select(TenantUser))).scalar_one()
        ids = dict(user_id=membership.user_id, membership_id=membership.id, tenant_id=membership.tenant_id)
    legacy_access, legacy_refresh = create_access_token(**ids), create_refresh_token(**ids)

    assert (await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {legacy_access}"})).status_code in (200, 404)
    upgraded = (await client.post("/api/v1/auth/refresh", json={"refresh_token": legacy_refresh})).json()
    listed = await client.get("/api/v1/auth/sessions", headers=_h(upgraded))
    assert listed.status_code == 200
    assert sum(s["current"] for s in listed.json()) == 1


async def test_password_reset_signs_every_device_out(client: AsyncClient, emails):
    from app.api.v1.auth import PASSWORD_RESET_PURPOSE
    from app.core.security import create_purpose_token, password_fingerprint

    await _signup(client, "reset-co")
    tokens = await _login(client, "reset-co")
    async with session_scope() as db:
        user = (await db.execute(select(User))).scalar_one()
        token = create_purpose_token(
            user_id=user.id,
            purpose=PASSWORD_RESET_PURPOSE,
            expires_delta=timedelta(hours=1),
            fingerprint=password_fingerprint(user.hashed_password),
        )
    done = await client.post("/api/v1/auth/reset-password", json={"token": token, "new_password": "anotherpass9"})
    assert done.status_code == 200, done.text
    assert (await client.get("/api/v1/auth/sessions", headers=_h(tokens))).status_code == 401


async def test_new_device_sign_in_sends_one_alert_but_known_devices_and_first_logins_do_not(
    client: AsyncClient, emails
):
    await _signup(client, "alert-co")  # first session: nothing to compare with
    await _login(client, "alert-co")  # same browser as signup: known
    assert [s for _, s in emails if s == "New sign-in to your account"] == []

    await _login(client, "alert-co", FIREFOX_WIN)  # a new kind of device
    alerts = [s for _, s in emails if s == "New sign-in to your account"]
    assert len(alerts) == 1
    await _login(client, "alert-co", FIREFOX_WIN)  # now known
    assert len([s for _, s in emails if s == "New sign-in to your account"]) == 1


async def test_a_failing_alert_email_never_blocks_signing_in(client: AsyncClient, monkeypatch):
    async def boom(**kw):
        raise RuntimeError("smtp down")

    await _signup(client, "boom-co")
    await _login(client, "boom-co")
    monkeypatch.setattr(user_sessions, "send_email", boom)
    tokens = await _login(client, "boom-co", FIREFOX_WIN)
    assert tokens["access_token"]


# --- API keys -----------------------------------------------------------------------------------


async def _make_key(client: AsyncClient, owner: dict, **extra) -> dict:
    resp = await client.post(
        "/api/v1/developer/api-keys",
        headers=owner,
        json={"name": "ci", "mode": "live", "scopes": ["calls:read"], **extra},
    )
    return resp


async def test_key_ip_limits_are_validated_normalised_and_enforced(client: AsyncClient, emails):
    owner = _h(await _signup(client, "ipkey-co"))
    bad = await _make_key(client, owner, allowed_ips=["not-an-ip"])
    assert bad.status_code == 422
    too_many = await _make_key(client, owner, allowed_ips=[f"10.0.{n}.1" for n in range(21)])
    assert too_many.status_code == 422

    elsewhere = (await _make_key(client, owner, allowed_ips=["10.0.0.0/8", "10.0.0.5"])).json()
    assert elsewhere["allowed_ips"] == ["10.0.0.0/8", "10.0.0.5/32"]
    denied = await client.get("/api/v1/calls", headers={"Authorization": f"Bearer {elsewhere['secret']}"})
    assert denied.status_code == 403 and "IP address" in denied.json()["detail"]

    here = (await _make_key(client, owner, allowed_ips=["127.0.0.1"])).json()
    allowed = await client.get("/api/v1/calls", headers={"Authorization": f"Bearer {here['secret']}"})
    assert allowed.status_code == 200

    unrestricted = (await _make_key(client, owner)).json()
    assert unrestricted["allowed_ips"] == []
    assert (await client.get("/api/v1/calls", headers={"Authorization": f"Bearer {unrestricted['secret']}"})).status_code == 200


async def test_editing_a_key_changes_limits_but_never_the_secret(client: AsyncClient, emails):
    owner = _h(await _signup(client, "edit-co"))
    key = (await _make_key(client, owner)).json()
    changed = await client.patch(
        f"/api/v1/developer/api-keys/{key['id']}",
        headers=owner,
        json={"name": "renamed", "allowed_ips": ["203.0.113.0/24"]},
    )
    assert changed.status_code == 200 and changed.json()["name"] == "renamed"
    assert changed.json()["allowed_ips"] == ["203.0.113.0/24"]
    assert "secret" not in changed.json()
    # a past expiry is refused, and a bad range is refused
    assert (await client.patch(f"/api/v1/developer/api-keys/{key['id']}", headers=owner, json={"expires_at": "2020-01-01T00:00:00Z"})).status_code == 400
    assert (await client.patch(f"/api/v1/developer/api-keys/{key['id']}", headers=owner, json={"allowed_ips": ["x"]})).status_code == 422
    await client.delete(f"/api/v1/developer/api-keys/{key['id']}", headers=owner)
    assert (await client.patch(f"/api/v1/developer/api-keys/{key['id']}", headers=owner, json={"name": "late"})).status_code == 409


async def test_rotation_keeps_the_old_key_for_a_grace_period_then_it_stops(client: AsyncClient, emails):
    owner = _h(await _signup(client, "rot-co"))
    old = (await _make_key(client, owner, allowed_ips=["127.0.0.1"])).json()
    rotated = await client.post(f"/api/v1/developer/api-keys/{old['id']}/rotate", headers=owner, json={"grace_hours": 12})
    assert rotated.status_code == 201, rotated.text
    new = rotated.json()
    assert new["id"] != old["id"] and new["secret"] != old["secret"]
    assert new["scopes"] == old["scopes"] and new["allowed_ips"] == ["127.0.0.1/32"] and new["name"] == "ci"

    for secret in (old["secret"], new["secret"]):  # both work during the grace period
        assert (await client.get("/api/v1/calls", headers={"Authorization": f"Bearer {secret}"})).status_code == 200
    keys = {k["id"]: k for k in (await client.get("/api/v1/developer/api-keys", headers=owner)).json()}
    ends = datetime.fromisoformat(keys[old["id"]]["expires_at"])
    assert timedelta(hours=11) < ends - datetime.now(UTC) < timedelta(hours=12, minutes=1)

    now_dead = await client.post(f"/api/v1/developer/api-keys/{new['id']}/rotate", headers=owner, json={"grace_hours": 0})
    older = {k["id"]: k for k in (await client.get("/api/v1/developer/api-keys", headers=owner)).json()}
    assert older[new["id"]]["revoked_at"] is not None
    assert (await client.get("/api/v1/calls", headers={"Authorization": f"Bearer {new['secret']}"})).status_code == 401
    assert now_dead.status_code == 201
    assert (await client.post(f"/api/v1/developer/api-keys/{new['id']}/rotate", headers=owner, json={})).status_code == 409


async def test_rotation_never_extends_a_key_that_was_about_to_expire(client: AsyncClient, emails):
    owner = _h(await _signup(client, "short-co"))
    soon = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    key = (await _make_key(client, owner, expires_at=soon)).json()
    await client.post(f"/api/v1/developer/api-keys/{key['id']}/rotate", headers=owner, json={"grace_hours": 100})
    keys = {k["id"]: k for k in (await client.get("/api/v1/developer/api-keys", headers=owner)).json()}
    ends = datetime.fromisoformat(keys[key["id"]]["expires_at"])
    assert ends - datetime.now(UTC) < timedelta(hours=2, minutes=1)


async def test_owners_are_emailed_about_live_keys_but_not_test_keys(client: AsyncClient, emails):
    owner = _h(await _signup(client, "mail-co"))
    await _make_key(client, owner)  # live
    await client.post("/api/v1/developer/api-keys", headers=owner, json={"name": "t", "mode": "test", "scopes": ["calls:read"]})
    subjects = [s for _, s in emails]
    assert subjects.count("A new live API key was created") == 1
    assert all("owner@mail-co.com" == to for to, s in emails if "API key" in s)


async def test_mcp_enforces_key_and_workspace_ip_limits(client: AsyncClient, emails):
    owner = _h(await _signup(client, "mcp-co"))
    key = (await _make_key(client, owner, allowed_ips=["10.0.0.0/8"])).json()
    headers = {"authorization": f"Bearer {key['secret']}"}
    at = lambda ip: SimpleNamespace(client=SimpleNamespace(host=ip), headers={})  # noqa: E731

    async with session_scope() as db:
        principal = await resolve_principal(headers, db, at("10.1.2.3"))
        assert principal.key_id
    async with session_scope() as db:
        with pytest.raises(McpAuthError, match="IP address"):
            await resolve_principal(headers, db, at("203.0.113.9"))

    # the workspace-wide allowlist now covers MCP too
    free = (await _make_key(client, owner)).json()
    free_headers = {"authorization": f"Bearer {free['secret']}"}
    async with session_scope() as db:
        tenant = (await db.execute(select(Tenant))).scalar_one()
        db.add(TenantSecuritySettings(tenant_id=tenant.id, ip_allowlist=["198.51.100.0/24"]))
        await db.commit()
    invalidate_allowlist_cache()  # the settings API does this; a raw insert has to
    async with session_scope() as db:
        with pytest.raises(McpAuthError, match="allowlist"):
            await resolve_principal(free_headers, db, at("203.0.113.9"))
    async with session_scope() as db:
        assert (await resolve_principal(free_headers, db, at("198.51.100.7"))).key_id


# --- Audit log ----------------------------------------------------------------------------------


async def test_audit_log_filters_by_action_prefix_type_and_time(client: AsyncClient, emails):
    owner = _h(await _signup(client, "aud-co"))
    key = (await _make_key(client, owner)).json()
    await client.post(f"/api/v1/developer/api-keys/{key['id']}/rotate", headers=owner, json={"grace_hours": 1})
    await client.patch("/api/v1/cost-controls", headers=owner, json={"monthly_budget": 100})

    everything = (await client.get("/api/v1/audit-logs", headers=owner)).json()
    actions = {row["action"] for row in everything}
    assert {"api_key.created", "api_key.rotated", "cost_controls.update"} <= actions

    keys_only = (await client.get("/api/v1/audit-logs", headers=owner, params={"action": "api_key"})).json()
    assert {row["action"] for row in keys_only} == {"api_key.created", "api_key.rotated"}
    exact = (await client.get("/api/v1/audit-logs", headers=owner, params={"action": "api_key.rotated"})).json()
    assert [row["action"] for row in exact] == ["api_key.rotated"]
    by_type = (await client.get("/api/v1/audit-logs", headers=owner, params={"resource_type": "cost_controls"})).json()
    assert [row["action"] for row in by_type] == ["cost_controls.update"]
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    assert (await client.get("/api/v1/audit-logs", headers=owner, params={"since": future})).json() == []
    assert len((await client.get("/api/v1/audit-logs", headers=owner, params={"until": future})).json()) == len(everything)
    # "%" and "_" are literal characters, never wildcards
    assert (await client.get("/api/v1/audit-logs", headers=owner, params={"action": "%"})).json() == []


async def test_audit_export_is_csv_safe_audited_and_tenant_scoped(client: AsyncClient, emails):
    owner = _h(await _signup(client, "exp-co"))
    other = _h(await _signup(client, "exp-other"))
    await client.patch("/api/v1/cost-controls", headers=owner, json={"monthly_budget": 5})
    async with session_scope() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.slug == "exp-co"))).scalar_one()
        db.add(AuditLog(tenant_id=tenant.id, action="=cmd|' /C calc'!A0", resource_type="x", extra={}))
        await db.commit()

    resp = await client.get("/api/v1/audit-logs/export", headers=owner)
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/csv")
    assert 'filename="audit-log.csv"' in resp.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(resp.text)))
    assert rows[0] == ["time_utc", "actor", "action", "resource_type", "resource_id", "details"]
    body = rows[1:]
    assert any(r[2] == "cost_controls.update" and r[1] == "owner@exp-co.com" for r in body)
    assert all(not r[2].startswith("=") for r in body)  # spreadsheet formula neutralised
    assert any(r[2].startswith("'=") for r in body)
    assert not any("exp-other" in cell for r in body for cell in r)

    exported = (await client.get("/api/v1/audit-logs", headers=owner, params={"action": "audit.exported"})).json()
    assert len(exported) == 1
    others = list(csv.reader(io.StringIO((await client.get("/api/v1/audit-logs/export", headers=other)).text)))
    assert not any(r[2] == "cost_controls.update" for r in others[1:])


async def test_audit_export_refuses_api_keys_and_anonymous(client: AsyncClient, emails):
    owner = _h(await _signup(client, "exp2-co"))
    key = (await client.post("/api/v1/developer/api-keys", headers=owner, json={"name": "k", "mode": "test", "scopes": ["calls:read"]})).json()
    assert (await client.get("/api/v1/audit-logs/export")).status_code == 401
    assert (await client.get("/api/v1/audit-logs/export", headers={"Authorization": f"Bearer {key['secret']}"})).status_code in (401, 403)


