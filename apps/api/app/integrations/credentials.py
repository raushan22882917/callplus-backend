"""Resolves which Twilio/Sarvam credentials to use for a given tenant:
their own (if they've connected one via POST /api/v1/integrations/{provider})
or the platform's shared one. This is what makes the platform usable by many
tenants without every tenant needing their own Twilio/Sarvam account, while
still letting an enterprise tenant bring their own for billing/data-residency
reasons.

Every call site that talks to Twilio or Sarvam on behalf of a specific
tenant should go through this module rather than reading app.core.config
directly — that's what "many tenants" actually requires structurally.
"""

import uuid
from dataclasses import dataclass

from app.core.config import get_settings
from app.core.crypto import decrypt_secret
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Integration, IntegrationCredential, IntegrationStatus


@dataclass(frozen=True)
class TwilioCredentials:
    account_sid: str
    auth_token: str


async def _connected_integration(
    db: AsyncSession, tenant_id: uuid.UUID, provider: str
) -> tuple[Integration, str] | None:
    result = await db.execute(
        select(Integration, IntegrationCredential)
        .join(IntegrationCredential, IntegrationCredential.integration_id == Integration.id)
        .where(
            Integration.tenant_id == tenant_id,
            Integration.provider == provider,
            Integration.status == IntegrationStatus.CONNECTED,
        )
    )
    row = result.first()
    if row is None:
        return None
    integration, credential = row
    return integration, decrypt_secret(credential.encrypted_secret)


async def uses_platform_twilio(db: AsyncSession, tenant_id: uuid.UUID) -> bool:
    """True when this tenant has not connected its own Twilio account, so a
    purchase or a call would bill the platform's account."""
    return await _connected_integration(db, tenant_id, "twilio") is None


async def get_twilio_credentials(db: AsyncSession, tenant_id: uuid.UUID) -> TwilioCredentials:
    connected = await _connected_integration(db, tenant_id, "twilio")
    if connected is not None:
        integration, auth_token = connected
        return TwilioCredentials(
            account_sid=integration.config["account_sid"], auth_token=auth_token
        )

    settings = get_settings()
    return TwilioCredentials(
        account_sid=settings.twilio_account_sid, auth_token=settings.twilio_auth_token
    )


class SharedAccountError(Exception):
    """The workspace has not connected its own account, so the shared one must not be billed."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


OWN_ACCOUNT_MESSAGE = (
    "Connect your own account under Integrations. For an Indian number, connect Exotel and "
    "add the ExoPhone you bought there. For any other number, connect Twilio. Nothing is "
    "placed or bought on a shared account."
)


async def require_own_twilio(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Refuse when this call or message would be billed to the shared Twilio account."""
    if not await uses_platform_twilio(db, tenant_id):
        return
    settings = get_settings()
    if settings.twilio_account_sid and settings.twilio_auth_token:
        raise SharedAccountError(OWN_ACCOUNT_MESSAGE)


async def get_sarvam_api_key(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    """The workspace's own Sarvam key. Empty when they have not connected one.

    The platform key is not used for a workspace. Each business pastes its own key
    so its calls are billed to its own Sarvam account.
    """
    connected = await _connected_integration(db, tenant_id, "sarvam")
    if connected is None:
        return ""
    return connected[1]
