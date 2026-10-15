"""Meta (Facebook Login) -> Messenger / Instagram channel wiring.

After the tenant authorizes Meta we list their Pages (and the Instagram
business accounts linked to them); when they pick one we write the existing
`channel_settings` rows (messenger / instagram) with the page token and
subscribe the Page to our app's webhook fields, so the normal inbound flow in
app/api/v1/channels.py works without pasting any token."""

import logging
import uuid
from typing import Any

import httpx
from app.core.config import get_settings
from app.integrations.oauth.errors import OAuthError
from app.integrations.oauth.http import new_client
from app.integrations.oauth.ownership import other_tenant_owns
from app.services import channels as channel_svc
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.channels import ChannelSettings

logger = logging.getLogger(__name__)

SUBSCRIBED_FIELDS = "messages,messaging_postbacks,message_reads,message_deliveries"
PAGE_FIELDS = "id,name,category,access_token,instagram_business_account{id,username,name}"


def _graph() -> str:
    return f"https://graph.facebook.com/{get_settings().meta_graph_api_version}"


def _err(response: httpx.Response) -> str:
    try:
        err = response.json().get("error", {})
        return str(err.get("message") or response.text[:200])
    except ValueError:
        return response.text[:200]


async def fetch_pages(client: httpx.AsyncClient, user_token: str) -> list[dict[str, Any]]:
    """Raw /me/accounts rows (include page access tokens: never return these to the browser)."""
    pages: list[dict[str, Any]] = []
    url: str | None = f"{_graph()}/me/accounts"
    params: dict[str, str] | None = {"fields": PAGE_FIELDS, "limit": "100", "access_token": user_token}
    for _ in range(5):  # bounded pagination
        try:
            response = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise OAuthError("provider_error", f"Could not list Facebook Pages: {exc}") from exc
        if response.status_code >= 400:
            raise OAuthError("provider_error", f"Could not list Facebook Pages: {_err(response)}")
        body = response.json()
        pages.extend(body.get("data", []))
        url = (body.get("paging") or {}).get("next")
        params = None
        if not url:
            break
    return pages


def public_pages(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for page in pages:
        ig = page.get("instagram_business_account") or None
        out.append(
            {
                "id": page["id"],
                "name": page.get("name"),
                "category": page.get("category"),
                "instagram": {"id": ig["id"], "username": ig.get("username") or ig.get("name")}
                if ig
                else None,
            }
        )
    return out


async def _subscribe(client: httpx.AsyncClient, page_id: str, page_token: str) -> None:
    try:
        response = await client.post(
            f"{_graph()}/{page_id}/subscribed_apps",
            data={"subscribed_fields": SUBSCRIBED_FIELDS, "access_token": page_token},
        )
    except httpx.HTTPError as exc:
        raise OAuthError("subscribe_failed", f"Could not subscribe the Page to webhooks: {exc}") from exc
    if response.status_code >= 400 or response.json().get("success") is False:
        raise OAuthError(
            "subscribe_failed", f"Could not subscribe the Page to webhooks: {_err(response)}"
        )


async def _claim_row(
    db: AsyncSession, tenant_id: uuid.UUID, channel: str, external_id: str, page_token: str
) -> ChannelSettings:
    clash = await other_tenant_owns(
        ChannelSettings, tenant_id, channel=channel, external_id=external_id
    )
    if clash:
        raise OAuthError(
            "account_in_use", "That account is already connected to another workspace", status_code=409
        )
    row = await channel_svc.get_channel_settings(db, tenant_id, channel)
    if row is None:
        row = ChannelSettings(tenant_id=tenant_id, channel=channel, config={})
        db.add(row)
    credentials = channel_svc.load_credentials(row)
    credentials["page_access_token"] = page_token
    row.encrypted_credentials = channel_svc.dump_credentials(credentials)
    row.external_id = external_id
    row.enabled = True
    return row


async def connect_page(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user_token: str,
    page_id: str,
    *,
    messenger: bool = True,
    instagram: bool = False,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    if not messenger and not instagram:
        raise OAuthError("invalid_selection", "Select Messenger and/or Instagram to connect")
    own = client is None
    http = client or new_client()
    try:
        pages = await fetch_pages(http, user_token)
        page = next((p for p in pages if p["id"] == page_id), None)
        if page is None:
            raise OAuthError("page_not_found", "That Page is not available to the connected Facebook account")
        page_token = page.get("access_token")
        if not page_token:
            raise OAuthError(
                "page_not_found",
                "Facebook returned no Page access token; reconnect and grant access to this Page",
            )
        ig = page.get("instagram_business_account") or None
        if instagram and not ig:
            raise OAuthError(
                "no_instagram_account",
                "This Page has no linked Instagram professional account",
            )
        await _subscribe(http, page_id, page_token)
    finally:
        if own:
            await http.aclose()

    selected: dict[str, Any] = {
        "page_id": page_id,
        "page_name": page.get("name"),
        "messenger": messenger,
        "instagram": instagram,
        "instagram_id": ig["id"] if ig else None,
        "instagram_username": (ig.get("username") or ig.get("name")) if ig else None,
    }
    if messenger:
        await _claim_row(db, tenant_id, "messenger", page_id, page_token)
    if instagram and ig:
        await _claim_row(db, tenant_id, "instagram", ig["id"], page_token)
    await db.flush()
    return selected


async def detach_all(
    db: AsyncSession, tenant_id: uuid.UUID, user_token: str, *, client: httpx.AsyncClient | None = None
) -> None:
    """On disconnect: switch off the channels this connection created and
    unsubscribe the Page. Best effort on the Graph side."""
    from app.integrations.oauth.tokens import get_connection

    conn = await get_connection(db, tenant_id, "meta")
    selected = (conn.extra or {}).get("selected") if conn else None
    if not selected:
        return
    ids = {selected.get("page_id"), selected.get("instagram_id")} - {None}
    rows = (
        await db.execute(
            select(ChannelSettings).where(
                ChannelSettings.tenant_id == tenant_id,
                ChannelSettings.channel.in_(("messenger", "instagram")),
            )
        )
    ).scalars().all()
    page_token = None
    for row in rows:
        if row.external_id in ids:
            page_token = page_token or channel_svc.load_credentials(row).get("page_access_token")
            row.enabled = False
            row.encrypted_credentials = None
            row.external_id = None
    if page_token and selected.get("page_id"):
        own = client is None
        http = client or new_client()
        try:
            await http.delete(
                f"{_graph()}/{selected['page_id']}/subscribed_apps",
                params={"access_token": page_token},
            )
        except Exception:
            logger.warning("could not unsubscribe page %s", selected.get("page_id"), exc_info=True)
        finally:
            if own:
                await http.aclose()
