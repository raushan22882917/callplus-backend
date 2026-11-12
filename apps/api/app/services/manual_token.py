"""Connect HubSpot, Shopify or Slack with an API token instead of the sign-in flow.

Some businesses cannot, or prefer not to, approve the platform's app: their IT admin has to, or the platform has not
registered an app with that provider yet. These providers let an owner create their own private app or bot and copy
its token. The token is checked with the provider right now, and only then saved, encrypted, as an ordinary
connection. Everything that already uses these connections (lead push, order lookup, Slack alerts) keeps working
without knowing how the connection was made.

The token is never logged and never sent back to the browser.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from app.integrations.oauth.errors import OAuthError
from app.integrations.oauth.flow import _after_connect
from app.integrations.oauth.http import new_client
from app.integrations.oauth.providers import SHOPIFY_API_VERSION, Identity, ShopifyProvider, TokenResult
from app.integrations.oauth.tokens import upsert_connection
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.oauth import OAuthConnection

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenHelp:
    label: str
    steps: str
    url: str
    placeholder: str


HELP: dict[str, TokenHelp] = {
    "hubspot": TokenHelp(
        "Private app access token",
        "In HubSpot open Settings, Integrations, Private Apps. Create an app with the CRM contacts and deals scopes, then copy its access token.",
        "https://developers.hubspot.com/docs/api/private-apps",
        "pat-na1-...",
    ),
    "shopify": TokenHelp(
        "Admin API access token",
        "In Shopify admin open Settings, Apps and sales channels, Develop apps. Create an app with the read_orders, read_customers and read_products scopes, install it, then reveal the Admin API access token.",
        "https://help.shopify.com/en/manual/apps/app-types/custom-apps",
        "shpat_...",
    ),
    "slack": TokenHelp(
        "Bot token",
        "Create a Slack app, add the chat:write, chat:write.public, channels:read and groups:read bot scopes, install it to your workspace, then copy the Bot User OAuth Token.",
        "https://api.slack.com/authentication/token-types#bot",
        "xoxb-...",
    ),
}
SUPPORTED = tuple(HELP)


def _refused(provider: str, label: str) -> OAuthError:
    return OAuthError("invalid_token", f"{label} did not accept this token. Copy it again and check it has the scopes listed above.", status_code=422)


def _unreachable(label: str) -> OAuthError:
    return OAuthError("provider_unreachable", f"We could not reach {label}. Please try again in a moment.", status_code=502)


async def _identify(client: httpx.AsyncClient, provider: str, token: str, shop: str | None) -> tuple[Identity, dict[str, Any]]:
    """Asks the provider who this token belongs to. Raises OAuthError when it will not say."""
    try:
        if provider == "hubspot":
            response = await client.get("https://api.hubapi.com/account-info/v3/details", headers={"Authorization": f"Bearer {token}"})
            if response.status_code in (401, 403):
                raise _refused(provider, "HubSpot")
            if response.status_code >= 400:
                raise _unreachable("HubSpot")
            data = response.json()
            portal = str(data.get("portalId")) if data.get("portalId") is not None else None
            return Identity(label=f"HubSpot portal {portal}" if portal else "HubSpot", external_id=portal), {}
        if provider == "slack":
            if not token.startswith("xoxb-"):
                raise OAuthError("invalid_token", "That is not a Slack bot token. It should start with xoxb-.", status_code=422)
            response = await client.post("https://slack.com/api/auth.test", headers={"Authorization": f"Bearer {token}"})
            if response.status_code >= 500:
                raise _unreachable("Slack")
            data = response.json()
            if not data.get("ok"):
                raise _refused(provider, "Slack")
            team = data.get("team") or "Slack workspace"
            return (
                Identity(label=team, external_id=data.get("team_id"), extra={"team_name": team, "bot_user_id": data.get("user_id")}),
                {"team_name": team, "team_id": data.get("team_id"), "bot_user_id": data.get("user_id")},
            )
        if provider == "shopify":
            try:
                domain = ShopifyProvider.normalize_shop(shop)
            except OAuthError as exc:
                raise OAuthError(exc.code, exc.message, status_code=422) from exc
            response = await client.get(f"https://{domain}/admin/api/{SHOPIFY_API_VERSION}/shop.json", headers={"X-Shopify-Access-Token": token})
            if response.status_code in (401, 403):
                raise _refused(provider, "Shopify")
            if response.status_code == 404:
                raise OAuthError("invalid_shop", "Shopify does not know that store. Check the store domain.", status_code=422)
            if response.status_code >= 400:
                raise _unreachable("Shopify")
            info = response.json().get("shop", {})
            name = info.get("name") or domain
            return Identity(label=f"{name} ({domain})", email=info.get("email"), external_id=domain, extra={"shop": domain}), {"shop": domain}
    except httpx.HTTPError as exc:
        raise _unreachable(provider.title()) from exc
    except ValueError as exc:  # the provider answered with something that is not JSON
        raise _unreachable(provider.title()) from exc
    raise OAuthError("unsupported", "An API token cannot be used for this provider.", status_code=400)


async def connect_with_token(
    db: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID | None, provider: str, token: str, shop: str | None = None
) -> OAuthConnection:
    """Checks the token with the provider, then saves it (encrypted) as this workspace's connection."""
    if provider not in SUPPORTED:
        raise OAuthError("unsupported", "An API token cannot be used for this provider.", status_code=400)
    token = (token or "").strip()
    if len(token) < 8:
        raise OAuthError("invalid_token", "Paste the whole token.", status_code=422)
    async with new_client() as client:
        identity, extra = await _identify(client, provider, token, shop)
    conn = await upsert_connection(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        provider=provider,
        token=TokenResult(access_token=token, refresh_token=None, expires_in=None, scopes=[], extra={**extra, "manual": True}),
        identity=identity,
        requested_scopes=[],
    )
    await db.commit()
    await _after_connect(db, provider, conn)
    return conn
