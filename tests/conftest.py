import os

os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://callcenter:callcenter@localhost:5433/callcenter_test"
)

# Tests must not depend on whatever a developer happens to have configured in
# their local .env (pydantic-settings reads it as a fallback, but a real
# process env var wins over it) — e.g. real Twilio credentials there would
# make _validate_request() actually check signatures against fake test
# webhook requests. Force every external-provider credential to "unset" for
# the whole test run regardless of local .env contents.
for _key in (
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "SARVAM_API_KEY",
    "RAZORPAY_KEY_ID",
    "RAZORPAY_KEY_SECRET",
    "SMTP_HOST",
):
    os.environ[_key] = ""

# Webhook signature checks fail closed when their secret is unset, so the
# tests sign with fixed non-empty secrets instead of "unset".
os.environ["META_WHATSAPP_APP_SECRET"] = "test-meta-app-secret"
os.environ["RAZORPAY_WEBHOOK_SECRET"] = "test-razorpay-webhook-secret"
os.environ["AUTH_RATE_LIMIT_ENABLED"] = "false"
os.environ["CAMPAIGN_VOICE_INTERVAL_SECONDS"] = "0"
os.environ["CAMPAIGN_WHATSAPP_INTERVAL_SECONDS"] = "0"

import asyncio
from collections.abc import AsyncIterator

import pytest
from app.core.config import get_settings
from app.core.permissions import ALL_PERMISSIONS_DESCRIPTIONS, DEFAULT_SYSTEM_ROLES
from app.main import app
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database import models  # noqa: F401 — populates Base.metadata
from packages.database.base import Base
from packages.database.models import Permission, Role
from packages.database.rls import apply_rls
from packages.database.session import get_engine, get_session, init_engine, session_scope


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


async def _seed_permissions_and_roles() -> None:
    async for session in get_session():
        permissions = {
            code: Permission(code=code, description=desc)
            for code, desc in ALL_PERMISSIONS_DESCRIPTIONS.items()
        }
        session.add_all(permissions.values())
        await session.flush()

        for role_name, codes in DEFAULT_SYSTEM_ROLES.items():
            role = Role(tenant_id=None, name=role_name, is_system=True)
            role.permissions = [permissions[code] for code in codes]
            session.add(role)

        await session.commit()
        return


@pytest.fixture(autouse=True)
async def _reset_database() -> AsyncIterator[None]:
    settings = get_settings()
    init_engine(settings.database_url)
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        if os.environ.get("TEST_RLS"):
            # Run the whole suite under row-level security (see
            # packages/database/rls.py) to prove no code path depends on
            # reading across tenants: `TEST_RLS=1 pytest`.
            await conn.run_sync(apply_rls)

    await _seed_permissions_and_roles()

    yield
    await engine.dispose()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session
