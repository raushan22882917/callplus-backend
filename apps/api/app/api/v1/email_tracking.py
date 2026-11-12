"""The two public addresses an email points at: the open pixel and the tracked link.

No login. The signed token says which workspace, campaign and person, and (for a link) the only address it may
send the reader to. A wrong or edited token gets the same image or a 404, and records nothing.
"""

import base64

from app.core.config import get_settings
from app.core.rate_limit_ip import ip_rate_limit
from app.services import email_tracking as svc
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.session import set_tenant_context

from app.core.deps import get_db

router = APIRouter(tags=["email-tracking"])

_PIXEL = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
_NO_STORE = {"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"}


@router.get(
    "/email/t/o/{token}.gif",
    include_in_schema=False,
    dependencies=[Depends(ip_rate_limit("email-open", limit=120))],
)
async def open_pixel(token: str, request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    parsed = svc.parse_token(token)
    agent = request.headers.get("user-agent")
    if parsed and parsed[3] is None and get_settings().email_tracking_enabled and not svc.looks_automated(agent):
        tenant_id, campaign_id, customer_id, _ = parsed
        await set_tenant_context(db, tenant_id)
        await svc.record(db, tenant_id=tenant_id, campaign_id=campaign_id, customer_id=customer_id, kind="open", user_agent=agent)
        await _commit_quietly(db)
    return Response(_PIXEL, media_type="image/gif", headers=_NO_STORE)


@router.get(
    "/email/t/c/{token}",
    include_in_schema=False,
    dependencies=[Depends(ip_rate_limit("email-click", limit=120))],
)
async def tracked_link(token: str, request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    parsed = svc.parse_token(token)
    if not parsed or parsed[3] is None:
        return Response("This link is not valid.", status_code=404, media_type="text/plain")
    tenant_id, campaign_id, customer_id, url = parsed
    agent = request.headers.get("user-agent")
    if get_settings().email_tracking_enabled and not svc.looks_automated(agent):
        await set_tenant_context(db, tenant_id)
        await svc.record(db, tenant_id=tenant_id, campaign_id=campaign_id, customer_id=customer_id, kind="click", url=url, user_agent=agent)
        await _commit_quietly(db)
    return RedirectResponse(url, status_code=302, headers=_NO_STORE)


async def _commit_quietly(db: AsyncSession) -> None:
    """A missing campaign (deleted since the email went out) must not break the reader's page or image."""
    try:
        await db.commit()
    except Exception:
        await db.rollback()
