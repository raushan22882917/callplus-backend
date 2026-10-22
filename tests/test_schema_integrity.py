"""Guards against the failures that only show up in production: a migration chain that
forks, a model column no migration creates, a tenant table without row-level security, and
an endpoint that was meant to need a login but does not.

Tests normally build tables from the models, so none of these would be noticed by any other
test. The schema test builds a database from the migrations, the way a deploy does."""

import asyncio
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from app.main import app
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from packages.database.base import Base

ROOT = Path(__file__).resolve().parents[1]


def test_there_is_one_migration_head():
    heads = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_heads()
    assert len(heads) == 1, f"migrations have forked: {heads}"


async def _migrated_database() -> tuple[str, str]:
    """Creates an empty database and runs every migration on it. Returns (admin url, name)."""
    base = make_url(os.environ["DATABASE_URL"])
    name = f"cc_integrity_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    await admin.dispose()
    env = {**os.environ, "DATABASE_URL": base.set(database=name).render_as_string(hide_password=False)}
    done = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
    )
    assert done.returncode == 0, done.stderr[-3000:]
    return env["DATABASE_URL"], name


async def _drop(name: str) -> None:
    admin = create_async_engine(
        make_url(os.environ["DATABASE_URL"]).set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    await admin.dispose()


async def test_the_migrated_database_matches_the_models_and_isolates_every_tenant_table():
    url, name = await _migrated_database()
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            def read(sync_conn):
                insp = inspect(sync_conn)
                tables = {
                    t: {c["name"]: c["nullable"] for c in insp.get_columns(t)}
                    for t in insp.get_table_names()
                    if t != "alembic_version"
                }
                rls = {
                    row[0]: (row[1], row[2])
                    for row in sync_conn.execute(
                        text("SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                             "WHERE relkind = 'r' AND relnamespace = 'public'::regnamespace")
                    )
                }
                policies = {
                    row[0]: row[1]
                    for row in sync_conn.execute(
                        text("SELECT tablename, count(*) FROM pg_policies GROUP BY tablename")
                    )
                }
                return tables, rls, policies

            tables, rls, policies = await conn.run_sync(read)
    finally:
        await engine.dispose()
        await _drop(name)

    problems: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            problems.append(f"table {table.name}: in the models but no migration creates it")
            continue
        for column in table.columns:
            if column.name not in tables[table.name]:
                problems.append(f"{table.name}.{column.name}: in the model but not in the migrated database")
            elif tables[table.name][column.name] != column.nullable:
                problems.append(f"{table.name}.{column.name}: nullable differs between model and migrations")
    for name_ in tables:
        if name_ not in Base.metadata.tables:
            problems.append(f"table {name_}: migrations create it but no model has it")
    for name_, columns in tables.items():
        if "tenant_id" in columns:
            enabled, forced = rls.get(name_, (False, False))
            if not (enabled and forced and policies.get(name_)):
                problems.append(f"{name_}: has tenant_id but row-level security is not fully on")
    assert not problems, "\n".join(problems)


# ---- every endpoint needs a login unless it is deliberately public --------------------------

# Public on purpose: they authenticate some other way (a signed webhook, a public key, a token
# in the body) or serve visitors who have no account. Adding a route here is a security decision.
PUBLIC = re.compile(
    r"^/api/v1/("
    r"auth/(signup|login|refresh|forgot-password|reset-password|verify-email|resend-verification|sso|oauth)"
    r"|channels/(sms/webhook|email/webhook|meta/webhook|forms|webchat|voicemail|click-to-call/bridge)"
    r"|webhooks/"
    r"|privacy/public/"
    r"|payments/(webhook|razorpay)"
    r"|whatsapp/(webhook|signup)"
    r"|internal/"          # shared-secret protected, called by our own realtime service
    r"|docs|openapi|health|ready|metrics"
    r"|oauth/"
    r")"
)
PUBLIC_EXACT = {"/api/v1/channels/webchat/widget.js", "/api/v1/channels/webchat/preview"}
# Single sign-on: the sign-in flow itself, before anyone has a session.
PUBLIC_SSO = re.compile(r"^/api/v1/platform/sso/\{tenant_slug\}/(status|login|callback)$")
# Twilio calls these mid-call. They are public to the internet but must refuse anything not
# signed by Twilio; the test below checks that they do, so this list cannot hide an open door.
SIGNED_WEBHOOKS = {
    ("POST", "/api/v1/telephony_plus/ivr/{menu_id}/digit"),
    ("POST", "/api/v1/telephony_plus/queues/{queue_id}/wait"),
    ("POST", "/api/v1/telephony_plus/queues/{queue_id}/result"),
    ("POST", "/api/v1/telephony_plus/queues/{queue_id}/agent"),
    ("POST", "/api/v1/telephony_plus/ring-groups/{group_id}/next"),
    ("POST", "/api/v1/telephony_plus/park/wait"),
    ("POST", "/api/v1/telephony_plus/park/{parked_id}/join"),
    ("POST", "/api/v1/telephony_plus/bridge/{call_id}"),
    ("POST", "/api/v1/workspace/webhooks/agent-leg"),
}


def _path(route_path: str) -> str:
    return re.sub(r"\{[^}]+\}", str(uuid.UUID(int=1)), route_path)


async def test_every_non_public_endpoint_refuses_anonymous_requests():
    verbs = {"get", "post", "put", "patch", "delete"}
    routes = [
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/api/v1")
        for method in operations
        if method in verbs
    ]
    assert len(routes) > 200, "route discovery found too little to be a meaningful check"
    open_routes = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for method, path in routes:
            if PUBLIC.match(path) or path in PUBLIC_EXACT or PUBLIC_SSO.match(path):
                continue
            if (method, path) in SIGNED_WEBHOOKS:
                continue
            resp = await client.request(method, _path(path), json={} if method != "GET" else None)
            if resp.status_code not in (401, 403):
                open_routes.append(f"{method} {path} -> {resp.status_code}")
    assert not open_routes, "endpoints reachable without signing in:\n" + "\n".join(open_routes)


async def test_signed_webhooks_refuse_unsigned_requests_when_not_running_locally(monkeypatch):
    """With no way to check a signature, a deployed server must say no, not skip the check."""
    from app.core.config import get_settings

    monkeypatch.setattr(type(get_settings()), "is_local", property(lambda self: False))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for method, path in sorted(SIGNED_WEBHOOKS):
            resp = await client.request(method, _path(path), data={"CallSid": "CA1"})
            assert resp.status_code in (403, 404), f"{method} {path} answered {resp.status_code} to an unsigned request"
            assert resp.status_code != 200
