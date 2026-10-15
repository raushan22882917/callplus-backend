"""Slack notifications through the tenant's OAuth bot token: called from the
existing connector dispatch (services/platform_connectors.dispatch_event), so
new tickets / SLA breaches reach the channel the tenant picked in the hub."""

import logging
import uuid
from typing import Any

import httpx
from app.integrations.oauth.errors import OAuthError
from app.integrations.oauth.http import new_client
from app.integrations.oauth.resources import SLACK_DEFAULT_EVENTS
from app.integrations.oauth.tokens import get_connection, get_valid_access_token, mark_status
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.oauth import OAuthConnectionStatus

logger = logging.getLogger(__name__)

_REVOKED_ERRORS = {"token_revoked", "invalid_auth", "account_inactive", "token_expired", "not_authed"}
_EVENT_TITLES = {
    "ticket.created": "New ticket",
    "sla.breached": "SLA breached",
    "call.ended": "Call ended",
    "campaign.completed": "Campaign completed",
}


async def slack_target(db: AsyncSession, tenant_id: uuid.UUID, event: str) -> str | None:
    """Channel id to post to when the tenant connected Slack, picked a channel
    and subscribed this event; otherwise None."""
    conn = await get_connection(db, tenant_id, "slack")
    if conn is None or conn.status != OAuthConnectionStatus.CONNECTED.value:
        return None
    selected = (conn.extra or {}).get("selected") or {}
    if not selected.get("channel_id"):
        return None
    if event not in (selected.get("events") or SLACK_DEFAULT_EVENTS):
        return None
    return selected["channel_id"]


async def post_slack(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    channel_id: str,
    event: str,
    data: dict[str, Any],
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    from app.services.platform_connectors import summarize_event

    result = {"connector_id": None, "type": "slack_oauth", "ok": False, "detail": ""}
    own = client is None
    client = client or new_client()  # shared factory: one timeout, mockable in tests
    try:
        token = await get_valid_access_token(db, tenant_id, "slack", client=client)
        text = f"*{_EVENT_TITLES.get(event, event)}*\n{summarize_event(event, data)}"
        response = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel_id, "text": text},
        )
        body = response.json() if response.content else {}
        if response.status_code >= 400 or not body.get("ok"):
            error = str(body.get("error") or f"HTTP {response.status_code}")
            if error in _REVOKED_ERRORS:
                await mark_status(
                    db, tenant_id, "slack", OAuthConnectionStatus.REVOKED, f"Slack: {error}"
                )
            result["detail"] = f"Slack rejected the message: {error}"
            return result
        result.update(ok=True, detail="Posted to Slack")
    except OAuthError as exc:
        result["detail"] = exc.message
    except (httpx.HTTPError, ValueError) as exc:
        result["detail"] = f"Slack request failed: {exc.__class__.__name__}"
    finally:
        if own:
            await client.aclose()
    return result
