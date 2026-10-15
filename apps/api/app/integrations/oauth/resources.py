"""Per-provider "which one do you want to use" pickers: Google/Outlook
calendars, Slack channels, Facebook Pages + linked Instagram accounts."""

import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from app.integrations.oauth.errors import OAuthError
from app.integrations.oauth.http import new_client
from app.integrations.oauth.providers import get_provider
from app.integrations.oauth.tokens import get_connection, get_valid_access_token
from sqlalchemy.ext.asyncio import AsyncSession

SLACK_EVENTS = ("ticket.created", "sla.breached", "call.ended", "campaign.completed")
SLACK_DEFAULT_EVENTS = ["ticket.created", "sla.breached"]


@dataclass
class SelectPayload:
    resource_id: str | None = None
    block_busy: bool | None = None
    messenger: bool | None = None
    instagram: bool | None = None
    events: list[str] | None = None
    extras: dict[str, Any] = field(default_factory=dict)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _get_json(client: httpx.AsyncClient, url: str, **kwargs: Any) -> dict[str, Any]:
    try:
        response = await client.get(url, **kwargs)
    except httpx.HTTPError as exc:
        raise OAuthError("provider_error", f"Request failed: {exc}") from exc
    if response.status_code in (401, 403):
        raise OAuthError("reconnect_required", "The provider rejected the stored token; reconnect")
    if response.status_code >= 400:
        raise OAuthError("provider_error", f"Provider returned HTTP {response.status_code}")
    return response.json()


async def _google_calendars(client: httpx.AsyncClient, token: str) -> list[dict[str, Any]]:
    data = await _get_json(
        client,
        "https://www.googleapis.com/calendar/v3/users/me/calendarList",
        params={"minAccessRole": "writer", "maxResults": "100"},
        headers=_bearer(token),
    )
    return [
        {
            "id": c["id"],
            "name": c.get("summary") or c["id"],
            "primary": bool(c.get("primary")),
            "detail": c.get("accessRole"),
        }
        for c in data.get("items", [])
    ]


async def _microsoft_calendars(client: httpx.AsyncClient, token: str) -> list[dict[str, Any]]:
    data = await _get_json(
        client,
        "https://graph.microsoft.com/v1.0/me/calendars",
        params={"$select": "id,name,isDefaultCalendar,canEdit", "$top": "50"},
        headers=_bearer(token),
    )
    return [
        {
            "id": c["id"],
            "name": c.get("name") or c["id"],
            "primary": bool(c.get("isDefaultCalendar")),
            "detail": "can edit" if c.get("canEdit", True) else "read only",
        }
        for c in data.get("value", [])
        if c.get("canEdit", True)
    ]


async def _slack_channels(client: httpx.AsyncClient, token: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(5):
        params = {
            "types": "public_channel,private_channel",
            "exclude_archived": "true",
            "limit": "200",
        }
        if cursor:
            params["cursor"] = cursor
        data = await _get_json(
            client, "https://slack.com/api/conversations.list", params=params, headers=_bearer(token)
        )
        if not data.get("ok"):
            raise OAuthError("provider_error", f"Slack error: {data.get('error')}")
        items.extend(
            {
                "id": c["id"],
                "name": f"#{c['name']}",
                "primary": False,
                "detail": "private" if c.get("is_private") else ("member" if c.get("is_member") else None),
            }
            for c in data.get("channels", [])
        )
        cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
    return items


async def list_resources(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    provider_key: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    provider = get_provider(provider_key)
    conn = await get_connection(db, tenant_id, provider_key)
    if conn is None:
        raise OAuthError("not_connected", f"{provider_key} is not connected", status_code=404)
    kind = provider.resources_kind
    selected = (conn.extra or {}).get("selected")
    if kind is None:
        return {"kind": "none", "items": [], "selected": selected}
    own = client is None
    http = client or new_client()
    try:
        token = await get_valid_access_token(db, tenant_id, provider_key, client=http)
        if provider_key == "google":
            items = await _google_calendars(http, token)
        elif provider_key == "microsoft":
            items = await _microsoft_calendars(http, token)
        elif provider_key == "slack":
            items = await _slack_channels(http, token)
        elif provider_key == "meta":
            from app.services import meta_channels

            items = meta_channels.public_pages(await meta_channels.fetch_pages(http, token))
        else:  # pragma: no cover - guarded by resources_kind
            items = []
    finally:
        if own:
            await http.aclose()
    return {"kind": kind, "items": items, "selected": selected}


async def select_resource(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    provider_key: str,
    payload: SelectPayload,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    provider = get_provider(provider_key)
    conn = await get_connection(db, tenant_id, provider_key)
    if conn is None:
        raise OAuthError("not_connected", f"{provider_key} is not connected", status_code=404)
    if provider.resources_kind is None:
        raise OAuthError("select_not_supported", f"{provider_key} has nothing to select")
    previous = dict((conn.extra or {}).get("selected") or {})
    own = client is None
    http = client or new_client()
    try:
        token = await get_valid_access_token(db, tenant_id, provider_key, client=http)
        if provider_key in ("google", "microsoft"):
            selected = previous
            if payload.resource_id:
                calendars = await (
                    _google_calendars if provider_key == "google" else _microsoft_calendars
                )(http, token)
                chosen = next((c for c in calendars if c["id"] == payload.resource_id), None)
                if chosen is None:
                    raise OAuthError("resource_not_found", "That calendar is not available")
                selected = {**previous, "calendar_id": chosen["id"], "calendar_name": chosen["name"]}
            elif "calendar_id" not in previous and payload.block_busy is None:
                raise OAuthError("invalid_selection", "Choose a calendar")
            if payload.block_busy is not None:
                selected = {**selected, "block_busy": bool(payload.block_busy)}
        elif provider_key == "slack":
            if not payload.resource_id and "channel_id" not in previous:
                raise OAuthError("invalid_selection", "Choose a channel")
            selected = previous
            if payload.resource_id:
                channels = await _slack_channels(http, token)
                chosen = next((c for c in channels if c["id"] == payload.resource_id), None)
                if chosen is None:
                    raise OAuthError("resource_not_found", "That channel is not available")
                selected = {**previous, "channel_id": chosen["id"], "channel_name": chosen["name"]}
            if payload.events is not None:
                unknown = [e for e in payload.events if e not in SLACK_EVENTS]
                if unknown:
                    raise OAuthError("invalid_selection", f"Unsupported event(s): {', '.join(unknown)}")
                selected = {**selected, "events": sorted(set(payload.events))}
            selected.setdefault("events", list(SLACK_DEFAULT_EVENTS))
        elif provider_key == "meta":
            from app.services import meta_channels

            if not payload.resource_id:
                raise OAuthError("invalid_selection", "Choose a Facebook Page")
            selected = await meta_channels.connect_page(
                db,
                tenant_id,
                token,
                payload.resource_id,
                messenger=True if payload.messenger is None else payload.messenger,
                instagram=bool(payload.instagram),
                client=http,
            )
        else:  # pragma: no cover
            raise OAuthError("select_not_supported", f"{provider_key} has nothing to select")
    finally:
        if own:
            await http.aclose()
    conn.extra = {**(conn.extra or {}), "selected": selected}
    await db.commit()
    return selected
