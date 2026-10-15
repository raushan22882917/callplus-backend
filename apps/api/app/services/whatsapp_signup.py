"""WhatsApp Embedded Signup: exchange the signup `code` the Facebook JS SDK
returns, subscribe our app to the tenant's WABA and store the number in
whatsapp_accounts (token encrypted), replacing manual token pasting.

Needs the platform to be a Meta Tech Provider (approved app + business
verification + an Embedded Signup configuration id): see docs/OAUTH_SETUP.md."""

import logging
import secrets
import uuid

import httpx
from app.core.config import get_settings
from app.core.crypto import encrypt_secret
from app.integrations.oauth.errors import OAuthError
from app.integrations.oauth.http import new_client
from app.integrations.oauth.ownership import other_tenant_owns
from app.services.whatsapp_inbound import default_agent_id
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import WhatsAppAccount

logger = logging.getLogger(__name__)


def _graph() -> str:
    return f"https://graph.facebook.com/{get_settings().meta_graph_api_version}"


def _err(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error", {}).get("message") or response.text[:200])
    except ValueError:
        return response.text[:200]


def signup_config() -> dict[str, str | bool]:
    s = get_settings()
    return {
        "configured": bool(s.meta_app_id and s.meta_oauth_secret and s.meta_whatsapp_config_id),
        "app_id": s.meta_app_id,
        "config_id": s.meta_whatsapp_config_id,
        "graph_api_version": s.meta_graph_api_version,
    }


async def complete_signup(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    code: str,
    phone_number_id: str,
    waba_id: str,
    pin: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[WhatsAppAccount, str | None]:
    s = get_settings()
    if not (s.meta_app_id and s.meta_oauth_secret):
        from app.integrations.oauth.errors import ProviderNotConfigured

        raise ProviderNotConfigured("whatsapp")
    own = client is None
    http = client or new_client()
    generated_pin: str | None = None
    try:
        try:
            token_resp = await http.get(
                f"{_graph()}/oauth/access_token",
                params={"client_id": s.meta_app_id, "client_secret": s.meta_oauth_secret, "code": code},
            )
        except httpx.HTTPError as exc:
            raise OAuthError("token_exchange_failed", f"Meta token request failed: {exc}") from exc
        if token_resp.status_code >= 400 or not token_resp.json().get("access_token"):
            raise OAuthError("token_exchange_failed", _err(token_resp))
        access_token = token_resp.json()["access_token"]
        auth = {"Authorization": f"Bearer {access_token}"}

        try:
            phone_resp = await http.get(
                f"{_graph()}/{phone_number_id}",
                params={"fields": "display_phone_number,verified_name"},
                headers=auth,
            )
        except httpx.HTTPError as exc:
            raise OAuthError("provider_error", f"Could not read the phone number: {exc}") from exc
        if phone_resp.status_code >= 400:
            raise OAuthError("provider_error", f"Could not read the phone number: {_err(phone_resp)}")
        display = phone_resp.json().get("display_phone_number") or phone_number_id

        sub = await http.post(f"{_graph()}/{waba_id}/subscribed_apps", headers=auth)
        if sub.status_code >= 400:
            raise OAuthError("subscribe_failed", f"Could not subscribe to WABA webhooks: {_err(sub)}")

        pin_to_use = pin
        if not pin_to_use:
            pin_to_use = generated_pin = f"{secrets.randbelow(10**6):06d}"
        reg = await http.post(
            f"{_graph()}/{phone_number_id}/register",
            headers=auth,
            json={"messaging_product": "whatsapp", "pin": pin_to_use},
        )
        if reg.status_code >= 400:
            # Already-registered numbers are fine; anything else is surfaced as a warning only.
            logger.warning("whatsapp register returned %s: %s", reg.status_code, _err(reg))
            generated_pin = None
    finally:
        if own:
            await http.aclose()

    if await other_tenant_owns(WhatsAppAccount, tenant_id, phone_number_id=phone_number_id):
        raise OAuthError(
            "account_in_use", "That WhatsApp number is already connected to another workspace", status_code=409
        )
    existing = (
        await db.execute(
            select(WhatsAppAccount).where(
                WhatsAppAccount.tenant_id == tenant_id,
                WhatsAppAccount.phone_number_id == phone_number_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = WhatsAppAccount(
            tenant_id=tenant_id,
            phone_number_id=phone_number_id,
            waba_id=waba_id,
            display_phone_number=display[:20],
            encrypted_access_token=encrypt_secret(access_token),
            ai_agent_id=await default_agent_id(db, tenant_id),
        )
        db.add(existing)
    else:
        existing.waba_id = waba_id
        existing.display_phone_number = display[:20]
        existing.encrypted_access_token = encrypt_secret(access_token)
    await db.flush()
    return existing, generated_pin
