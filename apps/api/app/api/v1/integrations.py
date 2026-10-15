"""Tenant self-service connection of their own (BYO) Twilio account or
Sarvam API key. Connecting one here means every call/message/turn for this
tenant uses their credentials instead of the platform's shared ones — see
app/integrations/credentials.py for where that's resolved."""

import uuid

from app.core.crypto import encrypt_secret
from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.schemas.integration import ConnectSarvamRequest, ConnectTwilioRequest, IntegrationRead
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
