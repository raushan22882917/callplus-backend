import hmac

from app.core.config import get_settings
from fastapi import Header, HTTPException, status


async def require_internal_service(x_internal_token: str | None = Header(default=None)) -> None:
    """Guards internal/* endpoints called by the Go realtime gateway (a
    trusted service, not a human user) — see app/api/v1/internal.py.

    This proves "the caller is our own realtime plane", nothing more. Every
    internal endpoint still derives tenant_id from the call/conversation row
    looked up by call_id, never from anything in the request body.
    """
    settings = get_settings()
    if not x_internal_token or not hmac.compare_digest(
        x_internal_token, settings.internal_service_token
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid internal service token")
