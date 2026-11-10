import uuid

from app.core.config import get_settings
from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.core.repository import TenantScopedRepository
from app.integrations.credentials import (
    OWN_ACCOUNT_MESSAGE,
    get_llm_key,
    get_provider_key,
    get_twilio_credentials,
    uses_platform_twilio,
)
from app.integrations import providers
from app.services.voice_clones import clone_for_voice, is_sarvam_clone_id, voice_allowed
from app.integrations.sarvam import SarvamAPIError, chat_completion
from app.integrations.twilio_client import (
    TwilioClientError,
    purchase_number,
    search_available_numbers,
)
from app.schemas.agent import (
    AgentFlowRead,
    AgentFlowSave,
    AIAgentCreate,
    AIAgentRead,
    AIAgentToolRead,
    AIAgentToolUpdate,
    AIAgentUpdate,
    AvailableNumber,
    PhoneNumberCreate,
    PhoneNumberProvision,
    PhoneNumberRead,
    PhoneNumberUpdate,
    TestChatRequest,
    TestChatResponse,
)
from app.services import exotel as exotel_service
from app.services import number_lifecycle, plans
from app.services.flow_compiler import compile_flow_to_system_prompt
from app.tools.registry import TOOL_REGISTRY
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.base.exceptions import TwilioException

from packages.database.models import AIAgent, AIAgentTool, AIAgentVersion, PhoneNumber

router = APIRouter(tags=["ai-agents"])


class AIAgentRepository(TenantScopedRepository[AIAgent]):
    model = AIAgent


class AIAgentToolRepository(TenantScopedRepository[AIAgentTool]):
    model = AIAgentTool


class PhoneNumberRepository(TenantScopedRepository[PhoneNumber]):
    model = PhoneNumber


async def _require_own_agent(db: AsyncSession, tenant_id: uuid.UUID, agent_id: uuid.UUID | None):
    """A number may only route to an agent of the SAME tenant. The FK alone
    would let one tenant point its number at another tenant's agent."""
    if agent_id is None:
        return
    await AIAgentRepository(db, tenant_id).get(agent_id)  # 404s if not this tenant's


async def _save_number(db: AsyncSession, tenant_id: uuid.UUID, **fields) -> PhoneNumber:
    """Create + commit, turning the globally-unique-number violation into a
    409 (a number can belong to only one tenant) instead of a 500."""
    try:
        number = await PhoneNumberRepository(db, tenant_id).create(**fields)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This phone number is already registered"
        ) from exc
    await db.refresh(number)
    return number


async def _check_speech_choice(
    db: AsyncSession, tenant_id: uuid.UUID, *, language: str, stt: str, tts: str, voice: str | None
) -> None:
    """An agent may use Deepgram or ElevenLabs only after the workspace connected its key, in a language the chosen
    providers handle, and ElevenLabs needs the ID of the voice to speak with."""
    if not providers.language_ok(language, stt=stt, tts=tts):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "This language needs Deepgram to hear and ElevenLabs to speak. Choose them, or pick an Indian language for Sarvam."
            if language in providers.INTERNATIONAL_LANGUAGES
            else "That language is not available with the chosen providers.",
        )
    for provider, label in ((providers.DEEPGRAM, "Deepgram"), (providers.ELEVENLABS, "ElevenLabs")):
        if provider in (stt, tts) and not await get_provider_key(db, tenant_id, provider):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Connect your {label} account under Integrations before choosing it for an agent.",
            )
    if tts == providers.SARVAM and is_sarvam_clone_id(voice) and await clone_for_voice(db, tenant_id, providers.SARVAM, voice) is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "That cloned voice is not available to this workspace."
        )
    if tts == providers.ELEVENLABS and not (voice or "").strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Choose an ElevenLabs voice for this agent.")
    if tts == providers.ELEVENLABS and not await voice_allowed(db, tenant_id, voice.strip()):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "That voice is not available to this workspace. Pick one from the list, or one you cloned.",
        )


@router.get("/ai-agents", response_model=list[AIAgentRead])
async def list_agents(
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[AIAgent]:
    return await AIAgentRepository(db, ctx.tenant_id).list()


@router.post("/ai-agents", response_model=AIAgentRead, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: AIAgentCreate,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> AIAgent:
    await plans.ensure_can_create(db, ctx.tenant_id, "agents")
    await _check_speech_choice(
        db, ctx.tenant_id, language=payload.language, stt=payload.stt_provider, tts=payload.tts_provider, voice=payload.voice
    )
    agent = await AIAgentRepository(db, ctx.tenant_id).create(**payload.model_dump())
    await db.commit()
    await db.refresh(agent)
    return agent


@router.get("/ai-agents/{agent_id}", response_model=AIAgentRead)
async def get_agent(
    agent_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> AIAgent:
    return await AIAgentRepository(db, ctx.tenant_id).get(agent_id)


@router.patch("/ai-agents/{agent_id}", response_model=AIAgentRead)
async def update_agent(
    agent_id: uuid.UUID,
    payload: AIAgentUpdate,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> AIAgent:
    repo = AIAgentRepository(db, ctx.tenant_id)
    agent = await repo.get(agent_id)
    changes = payload.model_dump(exclude_unset=True)
    if {"language", "stt_provider", "tts_provider", "voice"} & changes.keys():
        await _check_speech_choice(
            db,
            ctx.tenant_id,
            language=changes.get("language", agent.language),
            stt=changes.get("stt_provider", agent.stt_provider),
            tts=changes.get("tts_provider", agent.tts_provider),
            voice=changes.get("voice", agent.voice),
        )
    for field, value in changes.items():
        setattr(agent, field, value)
    await db.commit()
    await db.refresh(agent)
    return agent


@router.get("/ai-agents/{agent_id}/tools", response_model=list[AIAgentToolRead])
async def list_agent_tools(
    agent_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[AIAgentTool]:
    await AIAgentRepository(db, ctx.tenant_id).get(agent_id)  # 404s if not in this tenant
    return await AIAgentToolRepository(db, ctx.tenant_id).list(limit=len(TOOL_REGISTRY) or 50)


@router.put("/ai-agents/{agent_id}/tools", response_model=AIAgentToolRead)
async def enable_agent_tool(
    agent_id: uuid.UUID,
    payload: AIAgentToolUpdate,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> AIAgentTool:
    if payload.tool_name not in TOOL_REGISTRY:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown tool: {payload.tool_name}")
    await AIAgentRepository(db, ctx.tenant_id).get(agent_id)  # 404s if not in this tenant

    result = await db.execute(
        select(AIAgentTool).where(
            AIAgentTool.tenant_id == ctx.tenant_id,
            AIAgentTool.agent_id == agent_id,
            AIAgentTool.tool_name == payload.tool_name,
        )
    )
    tool_row = result.scalar_one_or_none()
    if tool_row is None:
        tool_row = AIAgentTool(
            tenant_id=ctx.tenant_id,
            agent_id=agent_id,
            tool_name=payload.tool_name,
            enabled=payload.enabled,
            configuration=payload.configuration,
        )
        db.add(tool_row)
    else:
        tool_row.enabled = payload.enabled
        tool_row.configuration = payload.configuration
    await db.commit()
    await db.refresh(tool_row)
    return tool_row


@router.get("/ai-agents/{agent_id}/flow", response_model=AgentFlowRead)
async def get_agent_flow(
    agent_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> AgentFlowRead:
    await AIAgentRepository(db, ctx.tenant_id).get(agent_id)  # 404s if not in this tenant

    result = await db.execute(
        select(AIAgentVersion)
        .where(AIAgentVersion.tenant_id == ctx.tenant_id, AIAgentVersion.agent_id == agent_id)
        .order_by(AIAgentVersion.version.desc())
        .limit(1)
    )
    latest = result.scalar_one_or_none()
    if latest is None:
        # No flow saved yet — hand back an empty shape rather than 404 so the
        # canvas can start from its own built-in starter nodes.
        return AgentFlowRead(version=0, published=False, nodes=[], edges=[])

    config = latest.configuration or {}
    return AgentFlowRead(
        version=latest.version,
        published=latest.published,
        nodes=config.get("nodes", []),
        edges=config.get("edges", []),
    )


@router.put("/ai-agents/{agent_id}/flow", response_model=AgentFlowRead)
async def save_agent_flow(
    agent_id: uuid.UUID,
    payload: AgentFlowSave,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> AgentFlowRead:
    agent = await AIAgentRepository(db, ctx.tenant_id).get(agent_id)

    latest_version = (
        await db.execute(
            select(AIAgentVersion.version)
            .where(AIAgentVersion.tenant_id == ctx.tenant_id, AIAgentVersion.agent_id == agent_id)
            .order_by(AIAgentVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none() or 0

    version = AIAgentVersion(
        tenant_id=ctx.tenant_id,
        agent_id=agent_id,
        version=latest_version + 1,
        configuration={"nodes": payload.nodes, "edges": payload.edges},
        published=payload.publish,
    )
    db.add(version)

    # The compiled prompt is what the live agent actually runs on the next
    # call/turn (see app/services/agent_engine.py) — saving a published flow
    # is not just a visual save, it changes real behavior immediately.
    if payload.publish:
        agent.system_prompt = compile_flow_to_system_prompt(payload.nodes, payload.edges)

    await db.commit()
    await db.refresh(version)
    return AgentFlowRead(
        version=version.version,
        published=version.published,
        nodes=payload.nodes,
        edges=payload.edges,
    )


@router.post("/ai-agents/{agent_id}/test-chat", response_model=TestChatResponse)
async def test_agent_chat(
    agent_id: uuid.UUID,
    payload: TestChatRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> TestChatResponse:
    """A real (but ephemeral, untooled) LLM turn for the Flow Studio "Test
    Agent" panel — nothing here is persisted to Message/Conversation, and no
    tools are executed, so a test run can never create a real payment link,
    send a real WhatsApp message, or transfer a real call. If nodes/edges
    are given, they're compiled fresh so you can test an unsaved edit;
    otherwise it uses the agent's currently saved system_prompt."""
    agent = await AIAgentRepository(db, ctx.tenant_id).get(agent_id)

    if payload.nodes is not None:
        system_prompt = compile_flow_to_system_prompt(payload.nodes, payload.edges or [])
    else:
        system_prompt = agent.system_prompt

    sarvam_api_key = await get_llm_key(db, ctx.tenant_id)
    history = [m.model_dump() for m in payload.messages]
    messages = [{"role": "system", "content": system_prompt}, *history]

    try:
        completion = await chat_completion(messages=messages, api_key=sarvam_api_key)
        reply = completion["choices"][0]["message"].get("content") or ""
    except SarvamAPIError as exc:
        raise HTTPException(status.HTTP_424_FAILED_DEPENDENCY, str(exc)) from exc

    return TestChatResponse(reply=reply)


@router.get("/phone-numbers", response_model=list[PhoneNumberRead])
async def list_phone_numbers(
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[PhoneNumber]:
    return await PhoneNumberRepository(db, ctx.tenant_id).list()


@router.post("/phone-numbers", response_model=PhoneNumberRead, status_code=status.HTTP_201_CREATED)
async def assign_phone_number(
    payload: PhoneNumberCreate,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> PhoneNumber:
    """Registers a number this tenant already has in Twilio (e.g. bought
    through their own BYO Twilio account, or ported from elsewhere) without
    buying a new one. To get a brand-new number instead, use
    GET /phone-numbers/available + POST /phone-numbers/provision below."""
    await plans.ensure_can_create(db, ctx.tenant_id, "phone_numbers")
    await _require_own_agent(db, ctx.tenant_id, payload.ai_agent_id)
    if (
        payload.provider == "exotel"
        and await exotel_service.get_credentials(db, ctx.tenant_id) is None
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Connect your Exotel account first (Integrations → Exotel).",
        )
    return await _save_number(db, ctx.tenant_id, **payload.model_dump())


@router.patch("/phone-numbers/{number_id}", response_model=PhoneNumberRead)
async def update_phone_number(
    number_id: uuid.UUID,
    payload: PhoneNumberUpdate,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> PhoneNumber:
    """Re-route inbound calls on this number to a different AI agent (or none)."""
    number = await PhoneNumberRepository(db, ctx.tenant_id).get(number_id)
    await _require_own_agent(db, ctx.tenant_id, payload.ai_agent_id)
    number.ai_agent_id = payload.ai_agent_id
    await db.commit()
    await db.refresh(number)
    return number


@router.delete("/phone-numbers/{number_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unregister_phone_number(
    number_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Stops routing the number. A number this platform bought is released at
    Twilio in the same step, so a deleted free number does not keep billing."""
    number = await PhoneNumberRepository(db, ctx.tenant_id).get(number_id)
    try:
        await number_lifecycle.release_if_platform_owned(db, number)
    except number_lifecycle.NumberReleaseError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await PhoneNumberRepository(db, ctx.tenant_id).delete(number_id)
    await db.commit()


@router.get("/phone-numbers/available", response_model=list[AvailableNumber])
async def search_phone_numbers(
    country: str = Query(default="US", min_length=2, max_length=2),
    area_code: str | None = Query(default=None, max_length=5),
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> list[AvailableNumber]:
    """Numbers this workspace could buy on its own Twilio account."""
    if await uses_platform_twilio(db, ctx.tenant_id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, OWN_ACCOUNT_MESSAGE)
    try:
        credentials = await get_twilio_credentials(db, ctx.tenant_id)
        numbers = search_available_numbers(
            country=country, area_code=area_code, credentials=credentials
        )
    except (TwilioClientError, TwilioException) as exc:
        raise HTTPException(status.HTTP_424_FAILED_DEPENDENCY, str(exc)) from exc
    return [AvailableNumber(**n) for n in numbers]


@router.post(
    "/phone-numbers/provision", response_model=PhoneNumberRead, status_code=status.HTTP_201_CREATED
)
async def provision_phone_number(
    payload: PhoneNumberProvision,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> PhoneNumber:
    """Buys the number on this workspace's own Twilio account and wires its
    voice webhook here. An Indian number is added from Exotel, not bought here."""
    await plans.ensure_can_create(db, ctx.tenant_id, "phone_numbers")  # before spending money
    await _require_own_agent(db, ctx.tenant_id, payload.ai_agent_id)  # before spending money
    on_platform = await uses_platform_twilio(db, ctx.tenant_id)
    if on_platform:
        await plans.ensure_platform_number_purchase(db, ctx.tenant_id)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, OWN_ACCOUNT_MESSAGE)
    settings = get_settings()
    voice_webhook_url = f"{settings.public_base_url}/api/v1/webhooks/telephony/twilio/voice"

    try:
        credentials = await get_twilio_credentials(db, ctx.tenant_id)
        provider_sid = purchase_number(
            phone_number=payload.phone_number,
            voice_webhook_url=voice_webhook_url,
            credentials=credentials,
        )
    except (TwilioClientError, TwilioException) as exc:
        raise HTTPException(status.HTTP_424_FAILED_DEPENDENCY, str(exc)) from exc

    return await _save_number(
        db,
        ctx.tenant_id,
        e164_number=payload.phone_number,
        provider_sid=provider_sid,
        ai_agent_id=payload.ai_agent_id,
        platform_owned=on_platform,
    )
