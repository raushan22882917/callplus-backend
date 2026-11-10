"""CallPlus's own provider accounts next to a workspace's own: who pays, who chooses, and what a call costs."""

import uuid
from datetime import UTC, datetime

import pytest
from app.core.config import get_settings
from app.integrations import managed
from app.integrations.credentials import get_llm_key, resolve_key, speech_settings
from app.integrations.llm import LlmEndpoint
from app.services import plans, wallet
from app.services.plans import PlanLimitError
from sqlalchemy import select
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    AIAgent,
    Call,
    CallDirection,
    CallStatus,
    Tenant,
    User,
    WalletTransaction,
)

from .test_channels import _signup


@pytest.fixture
def platform_accounts(monkeypatch: pytest.MonkeyPatch):
    """The platform owner has set its own keys and switched every provider on."""
    for name, value in {
        "SARVAM_API_KEY": "platform-sarvam",
        "DEEPGRAM_API_KEY": "platform-deepgram",
        "ELEVENLABS_API_KEY": "platform-eleven",
        "MANAGED_PROVIDERS": "sarvam,deepgram,elevenlabs,llm",
        "MANAGED_MINUTE_EXTRAS": "deepgram:0.2,elevenlabs:1,llm:0",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


async def _tenant_id(client: AsyncClient, auth: dict) -> uuid.UUID:
    return uuid.UUID((await client.get("/api/v1/tenants/me", headers=auth)).json()["id"])


async def _connect(client: AsyncClient, auth: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok(*_a, **_k):
        return None

    monkeypatch.setattr("app.api.v1.integrations.check_elevenlabs_key", ok)
    monkeypatch.setattr("app.api.v1.integrations.check_deepgram_key", ok)
    await client.post("/api/v1/integrations/elevenlabs", headers=auth, json={"api_key": "own-eleven"})
    await client.post("/api/v1/integrations/deepgram", headers=auth, json={"api_key": "own-deepgram"})


def test_nothing_is_shared_until_the_owner_switches_it_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPGRAM_API_KEY", "k")  # a key alone is not enough
    monkeypatch.setenv("MANAGED_PROVIDERS", "")
    get_settings.cache_clear()
    try:
        assert not managed.available("deepgram")
        monkeypatch.setenv("MANAGED_PROVIDERS", "deepgram")
        get_settings.cache_clear()
        assert managed.available("deepgram") and not managed.available("elevenlabs")
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_sarvam_is_offered_from_the_platform_key_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SARVAM_API_KEY", "platform-sarvam")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "k")
    monkeypatch.setenv("MANAGED_PROVIDERS", "")
    get_settings.cache_clear()
    try:
        assert managed.available("sarvam")
        assert not managed.available("deepgram") and not managed.available("elevenlabs")
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_extra_minutes_are_read_from_settings(platform_accounts) -> None:
    assert managed.extra_minutes("deepgram") == 0.2 and managed.extra_minutes("elevenlabs") == 1.0
    assert managed.extra_minutes("sarvam") == 0.0 and managed.extra_minutes("unknown") == 0.0


async def test_without_the_platform_switch_a_workspace_uses_only_its_own(client: AsyncClient, db_session: AsyncSession) -> None:
    auth = await _signup(client, slug="byo-only")
    tenant = await _tenant_id(client, auth)
    assert await resolve_key(db_session, tenant, "sarvam") == ("", "")
    modes = (await client.get("/api/v1/integrations/modes", headers=auth)).json()
    assert all(not m["managed_available"] for m in modes)
    refused = await client.put("/api/v1/integrations/modes", headers=auth, json={"provider": "deepgram", "mode": "managed"})
    assert refused.status_code == 422


async def test_callplus_account_is_used_unless_the_workspace_connects_or_chooses_its_own(
    client: AsyncClient, db_session: AsyncSession, platform_accounts, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = await _signup(client, slug="choice-co")
    tenant = await _tenant_id(client, auth)

    # Nothing connected: CallPlus's account.
    assert await resolve_key(db_session, tenant, "deepgram") == ("platform-deepgram", "managed")
    assert await resolve_key(db_session, tenant, "sarvam") == ("platform-sarvam", "managed")

    # The workspace connects its own: that wins by default.
    await _connect(client, auth, monkeypatch)
    db_session.expire_all()
    assert await resolve_key(db_session, tenant, "deepgram") == ("own-deepgram", "own")

    # And it can switch back to CallPlus's, then to its own again.
    state = (await client.put("/api/v1/integrations/modes", headers=auth, json={"provider": "deepgram", "mode": "managed"})).json()
    assert {s["provider"]: s["using"] for s in state}["deepgram"] == "managed"
    db_session.expire_all()
    assert await resolve_key(db_session, tenant, "deepgram") == ("platform-deepgram", "managed")
    back = await client.put("/api/v1/integrations/modes", headers=auth, json={"provider": "deepgram", "mode": "own"})
    assert back.status_code == 200
    db_session.expire_all()
    assert await resolve_key(db_session, tenant, "deepgram") == ("own-deepgram", "own")
    cleared = (await client.put("/api/v1/integrations/modes", headers=auth, json={"provider": "deepgram", "mode": None})).json()
    assert {s["provider"]: s["choice"] for s in cleared}["deepgram"] is None

    # "Own" cannot be chosen for a provider with no account of its own.
    nope = await client.put("/api/v1/integrations/modes", headers=auth, json={"provider": "llm", "mode": "own"})
    assert nope.status_code == 422


async def test_the_language_model_defaults_to_sarvam_and_can_be_a_separate_callplus_model(
    client: AsyncClient, db_session: AsyncSession, platform_accounts, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = await _signup(client, slug="llm-managed")
    tenant = await _tenant_id(client, auth)
    assert await get_llm_key(db_session, tenant) == "platform-sarvam"  # no separate model configured

    monkeypatch.setenv("MANAGED_LLM_API_KEY", "k-llm")
    monkeypatch.setenv("MANAGED_LLM_URL", "https://llm.example.com/v1/chat/completions")
    monkeypatch.setenv("MANAGED_LLM_MODEL", "fast-1")
    get_settings.cache_clear()
    key = await get_llm_key(db_session, tenant)
    assert isinstance(key, LlmEndpoint) and key.model == "fast-1" and key.label == "CallPlus AI"


async def test_an_agent_can_use_callplus_voices_and_the_call_counts_extra_minutes(
    client: AsyncClient, db_session: AsyncSession, platform_accounts, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def premade(_key):
        return {"v-es"}

    monkeypatch.setattr("app.services.voice_clones._premade_ids", premade)
    auth = await _signup(client, slug="premium-co")
    tenant = await _tenant_id(client, auth)
    # No keys of its own: allowed because CallPlus's accounts are offered.
    made = await client.post(
        "/api/v1/ai-agents", headers=auth,
        json={"name": "Line", "language": "es", "stt_provider": "deepgram", "tts_provider": "elevenlabs", "voice": "v-es"},
    )
    assert made.status_code == 201, made.text
    agent = await db_session.get(AIAgent, uuid.UUID(made.json()["id"]))
    speech = await speech_settings(db_session, tenant, agent)
    assert (speech["stt_api_key"], speech["tts_api_key"]) == ("platform-deepgram", "platform-eleven")
    assert speech["cost_multiplier"] == 2.2  # 1 + 0.2 (Deepgram) + 1 (ElevenLabs)

    call = Call(
        tenant_id=tenant, ai_agent_id=agent.id, direction=CallDirection.INBOUND, status=CallStatus.RINGING,
        from_number="web", to_number="web", started_at=datetime.now(UTC),
    )
    db_session.add(call)
    await db_session.commit()
    cfg = (await client.get(f"/api/v1/internal/calls/{call.id}/config", headers={"X-Internal-Token": get_settings().internal_service_token})).json()
    assert cfg["tts_api_key"] == "platform-eleven" and cfg["tts_model"] == "eleven_flash_v2_5"
    assert "cost_multiplier" not in cfg
    await db_session.refresh(call)
    assert float(call.cost_multiplier) == 2.2


async def test_the_plan_counts_a_premium_call_at_its_multiplier(client: AsyncClient, db_session: AsyncSession) -> None:
    auth = await _signup(client, slug="count-co")
    tenant = await _tenant_id(client, auth)
    agent = (await client.post("/api/v1/ai-agents", headers=auth, json={"name": "A"})).json()["id"]
    for seconds, mult in ((61, 1), (61, 2.5)):  # 2 minutes plain, then 2 minutes x 2.5
        db_session.add(Call(
            tenant_id=tenant, ai_agent_id=uuid.UUID(agent), direction=CallDirection.INBOUND, status=CallStatus.COMPLETED,
            duration_seconds=seconds, started_at=datetime.now(UTC), cost_multiplier=mult,
        ))
    await db_session.commit()
    assert await plans.billable_minutes_used(db_session, tenant, "lifetime") == 2 + 5


async def test_callplus_sarvam_uses_a_plan_minute_and_an_own_key_does_not(
    client: AsyncClient,
    db_session: AsyncSession,
    platform_accounts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def ok(*_a, **_k):
        return None

    monkeypatch.setattr("app.api.v1.integrations.check_api_key", ok)
    auth = await _signup(client, slug="two-prices")
    tenant = await _tenant_id(client, auth)
    made = await client.post("/api/v1/ai-agents", headers=auth, json={"name": "Desk"})
    assert made.status_code == 201, made.text
    agent_id = uuid.UUID(made.json()["id"])
    agent = await db_session.get(AIAgent, agent_id)

    included = await speech_settings(db_session, tenant, agent)
    assert included["stt_api_key"] == "platform-sarvam"
    assert included["cost_multiplier"] == 1.0

    saved = await client.post("/api/v1/integrations/sarvam", headers=auth, json={"api_key": "own-sarvam"})
    assert saved.status_code == 201, saved.text
    db_session.expire_all()
    agent = await db_session.get(AIAgent, agent_id)
    own = await speech_settings(db_session, tenant, agent)
    assert own["stt_api_key"] == "own-sarvam" and own["cost_multiplier"] == 0.0

    back = await client.put(
        "/api/v1/integrations/modes", headers=auth, json={"provider": "sarvam", "mode": "managed"}
    )
    assert back.status_code == 200
    db_session.expire_all()
    agent = await db_session.get(AIAgent, agent_id)
    speech = await speech_settings(db_session, tenant, agent)
    assert speech["cost_multiplier"] == 1.0

    row = await db_session.get(Tenant, tenant)
    row.plan_tier = "free"
    owner = (
        await db_session.execute(select(User).where(User.email == "owner@two-prices.co"))
    ).scalar_one()
    owner.email_verified_at = datetime.now(UTC)
    db_session.add(Call(
        tenant_id=tenant, ai_agent_id=agent_id, direction=CallDirection.INBOUND, status=CallStatus.COMPLETED,
        duration_seconds=600, started_at=datetime.now(UTC), cost_multiplier=1,
    ))
    await db_session.commit()
    with pytest.raises(PlanLimitError):
        await plans.ensure_minutes_available(db_session, tenant)
    await db_session.rollback()

    await client.put("/api/v1/integrations/modes", headers=auth, json={"provider": "sarvam", "mode": "own"})
    db_session.expire_all()
    await plans.ensure_minutes_available(db_session, tenant)
    assert await plans.billable_minutes_used(db_session, tenant, "lifetime") == 10

    opened = await wallet.ensure_wallet(db_session, tenant)
    await db_session.commit()
    db_session.add(Call(
        tenant_id=tenant, ai_agent_id=agent_id, direction=CallDirection.INBOUND, status=CallStatus.COMPLETED,
        duration_seconds=61, started_at=datetime.now(UTC), cost_multiplier=0,
    ))
    await db_session.commit()
    assert await wallet.settle(db_session, tenant) == 0
    tx = (
        await db_session.execute(select(WalletTransaction).where(WalletTransaction.tenant_id == tenant))
    ).scalars().all()
    assert tx[-1].amount_paise == 0 and "not charged" in tx[-1].description
    assert opened.balance_paise == 0
