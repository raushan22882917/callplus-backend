"""Deleting call audio at the telephony provider, so "delete" means the audio
is gone and not just that we stopped linking to it."""

import asyncio
import logging
import re
import uuid

from app.integrations import twilio_client
from app.integrations.credentials import get_twilio_credentials
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_RECORDING_SID = re.compile(r"/Recordings/(RE[0-9a-fA-F]{32})")


def recording_sid(url: str | None) -> str | None:
    match = _RECORDING_SID.search(url or "")
    return match.group(1) if match else None


async def delete_provider_recording(db: AsyncSession, tenant_id: uuid.UUID, url: str | None) -> bool:
    """True when the audio is gone (deleted now, already gone, or never held by
    the provider); False when the provider could not be reached, so the caller
    keeps the link and tries again next time."""
    sid = recording_sid(url)
    if sid is None:
        return True
    try:
        credentials = await get_twilio_credentials(db, tenant_id)
        await asyncio.to_thread(twilio_client.delete_recording, recording_sid=sid, credentials=credentials)
    except Exception:
        logger.warning("could not delete recording %s at the provider", sid, exc_info=True)
        return False
    return True
