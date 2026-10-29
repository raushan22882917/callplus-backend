"""Tenant self-service connection of their own (BYO) Twilio account or
Sarvam API key. Connecting one here means every call/message/turn for this
tenant uses their credentials instead of the platform's shared ones — see
app/integrations/credentials.py for where that's resolved."""

import uuid

from app.core.config import get_settings
from app.core.crypto import encrypt_secret
from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.schemas.integration import (
    ConnectExotelRequest,
    ConnectSarvamRequest,
    ConnectTwilioRequest,
    ExotelOutboundRequest,
    ExotelSetup,
    IntegrationRead,
)
from app.services import exotel as exotel_service
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Integration, IntegrationCredential, IntegrationStatus

router = APIRouter(prefix="/integrations", tags=["integrations"])


async def _upsert_integration(
    db: AsyncSession, *, tenant_id: uuid.UUID, provider: str, config: dict, secret: str
) -> Integration:
    result = await db.execute(
        select(Integration).where(
            Integration.tenant_id == tenant_id, Integration.provider == provider
        )
    )
    integration = result.scalar_one_or_none()

    if integration is None:
        integration = Integration(
            tenant_id=tenant_id,
            provider=provider,
            status=IntegrationStatus.CONNECTED,
            config=config,
        )
        db.add(integration)
        await db.flush()
        db.add(
            IntegrationCredential(
                tenant_id=tenant_id,
                integration_id=integration.id,
                encrypted_secret=encrypt_secret(secret),
            )
        )
        return integration

    integration.config = config
    integration.status = IntegrationStatus.CONNECTED
    cred_result = await db.execute(
        select(IntegrationCredential).where(IntegrationCredential.integration_id == integration.id)
    )
    credential = cred_result.scalar_one_or_none()
    if credential is None:
        db.add(
            IntegrationCredential(
                tenant_id=tenant_id,
                integration_id=integration.id,
                encrypted_secret=encrypt_secret(secret),
            )
        )
    else:
        credential.encrypted_secret = encrypt_secret(secret)
    return integration


@router.get("", response_model=list[IntegrationRead])
async def list_integrations(
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[Integration]:
    result = await db.execute(select(Integration).where(Integration.tenant_id == ctx.tenant_id))
    return list(result.scalars().all())


@router.get("/included")
async def included_services(
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_READ)),
) -> dict[str, bool]:
    """What every workspace gets without connecting anything: the platform's Sarvam AI key is used
    for a workspace that has not saved its own."""
    return {"sarvam": bool(get_settings().sarvam_api_key)}


@router.post("/twilio", response_model=IntegrationRead, status_code=status.HTTP_201_CREATED)
async def connect_twilio(
    payload: ConnectTwilioRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Integration:
    integration = await _upsert_integration(
        db,
        tenant_id=ctx.tenant_id,
        provider="twilio",
        config={"account_sid": payload.account_sid},
        secret=payload.auth_token,
    )
    await db.commit()
    await db.refresh(integration)
    return integration


@router.post("/sarvam", response_model=IntegrationRead, status_code=status.HTTP_201_CREATED)
async def connect_sarvam(
    payload: ConnectSarvamRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Integration:
    integration = await _upsert_integration(
        db, tenant_id=ctx.tenant_id, provider="sarvam", config={}, secret=payload.api_key
    )
    await db.commit()
    await db.refresh(integration)
    return integration


def _exotel_steps() -> list[str]:
    return [
        "In Exotel, open App Bazaar and create a new flow (or edit the one your number uses).",
        "Add a Voicebot applet and paste the stream address below into its URL box. "
        "Keep the sample rate at 8000.",
        "Add a Hangup applet after the Voicebot applet.",
        "Attach your Exotel number (ExoPhone) to this flow.",
        "In CallPulse, add the same number under Phone numbers, choose Exotel, "
        "and pick the AI agent.",
        "To place calls from this number too, copy the flow's App ID (next to it in App Bazaar) "
        "into Integrations → Exotel → Outgoing calls.",
    ]


@router.post("/exotel", response_model=ExotelSetup, status_code=status.HTTP_201_CREATED)
async def connect_exotel(
    payload: ConnectExotelRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> ExotelSetup:
    """Saves this workspace's own Exotel account after checking the details with Exotel, and
    returns the private stream address to paste into its Exotel flow (shown only now)."""
    try:
        await exotel_service.check_credentials(
            subdomain=payload.subdomain,
            account_sid=payload.account_sid.strip(),
            api_key=payload.api_key.strip(),
            api_token=payload.api_token.strip(),
        )
    except exotel_service.ExotelError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    integration, key = await exotel_service.connect(
        db,
        ctx.tenant_id,
        subdomain=payload.subdomain,
        account_sid=payload.account_sid.strip(),
        api_key=payload.api_key.strip(),
        api_token=payload.api_token.strip(),
    )
    await db.commit()
    await db.refresh(integration)
    return ExotelSetup(
        integration=integration, stream_url=exotel_service.stream_url(key), steps=_exotel_steps()
    )


@router.post("/exotel/stream-address", response_model=ExotelSetup)
async def new_exotel_stream_address(
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> ExotelSetup:
    """A fresh stream address; the old one stops working. For a leaked or lost address."""
    try:
        key = await exotel_service.regenerate_stream_key(db, ctx.tenant_id)
    except exotel_service.ExotelError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await db.commit()
    result = await db.execute(
        select(Integration).where(
            Integration.tenant_id == ctx.tenant_id, Integration.provider == "exotel"
        )
    )
    return ExotelSetup(
        integration=result.scalar_one(),
        stream_url=exotel_service.stream_url(key),
        steps=_exotel_steps(),
    )


@router.put("/exotel/outbound", response_model=IntegrationRead)
async def set_exotel_outbound_flow(
    payload: ExotelOutboundRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> Integration:
    """Saves which Exotel flow an answered outbound call runs, so the dialer can call from
    Exotel numbers."""
    try:
        integration = await exotel_service.set_outbound_app_id(db, ctx.tenant_id, payload.app_id)
    except exotel_service.ExotelError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    await db.commit()
    await db.refresh(integration)
    return integration


@router.delete("/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_integration(
    provider: str,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(
        select(Integration).where(
            Integration.tenant_id == ctx.tenant_id, Integration.provider == provider
        )
    )
    integration = result.scalar_one_or_none()
    if integration is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Integration not connected")
    await db.delete(integration)
    await db.commit()
