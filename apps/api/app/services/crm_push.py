"""Push a CRM lead to the tenant's connected HubSpot / Zoho / Salesforce.

Two entry points: `push_lead_safely` (best effort, called when a lead is
created; never raises) and `push_lead` (explicit, returns per-provider
results, used by the manual "push to CRM" endpoint). Tokens come from the
per-tenant OAuth manager, so they are refreshed automatically."""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from app.integrations.oauth.errors import OAuthError
from app.integrations.oauth.http import new_client
from app.integrations.oauth.providers import _SALESFORCE_HOST_RE, _ZOHO_API_RE
from app.integrations.oauth.tokens import get_connection, get_valid_access_token
from app.services import platform_connectors as pc
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.crm import CrmCompany, CrmLead
from packages.database.models.oauth import OAuthConnection, OAuthConnectionStatus

logger = logging.getLogger(__name__)

CRM_PROVIDERS = ("hubspot", "zoho", "salesforce")
PUSH_TIMEOUT = 8.0
SALESFORCE_API_VERSION = "v60.0"


async def _customer_dict(db: AsyncSession, lead: CrmLead) -> dict[str, Any]:
    company = None
    if lead.company_id:
        row = await db.get(CrmCompany, lead.company_id)
        company = row.name if row else None
    return {"name": lead.name, "email": lead.email, "phone": lead.phone, "company": company}


async def _hubspot(
    client: httpx.AsyncClient, token: str, customer: dict[str, Any], conn: OAuthConnection
) -> str:
    # Reuse the contact upsert already shared with the legacy connector.
    return await pc.send_hubspot(
        {"access_token": token}, "crm.lead_created", {}, customer, client
    )


async def _zoho(
    client: httpx.AsyncClient, token: str, customer: dict[str, Any], conn: OAuthConnection
) -> str:
    if not customer.get("email") and not customer.get("phone"):
        raise pc.ConnectorError("Lead has neither email nor phone")
    api = (conn.extra or {}).get("api_domain") or ""
    if not _ZOHO_API_RE.match(api):
        raise pc.ConnectorError("Zoho API domain is unknown; reconnect Zoho")
    response = await pc._call(
        client,
        "POST",
        f"{api}/crm/v2/Leads/upsert",
        headers={"Authorization": f"Zoho-oauthtoken {token}"},
        json=pc.zoho_lead_request(customer),
    )
    result = (response.json().get("data") or [{}])[0]
    if result.get("status") == "error":
        raise pc.ConnectorError(f"Zoho rejected the lead: {result.get('message')}")
    return f"Zoho lead {result.get('action', 'saved')}"


def _soql_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def salesforce_lead_fields(customer: dict[str, Any]) -> dict[str, Any]:
    first, last = pc.split_name(customer.get("name") or "")
    fields: dict[str, Any] = {
        "LastName": last or first or "Unknown",
        "Company": customer.get("company") or "Unknown",
        "LeadSource": "AutoNXT",
    }
    if last and first:
        fields["FirstName"] = first
    if customer.get("email"):
        fields["Email"] = customer["email"]
    if customer.get("phone"):
        fields["Phone"] = customer["phone"]
    return fields


async def _salesforce(
    client: httpx.AsyncClient, token: str, customer: dict[str, Any], conn: OAuthConnection
) -> str:
    instance = (conn.extra or {}).get("instance_url") or ""
    if not _SALESFORCE_HOST_RE.match(instance):
        raise pc.ConnectorError("Salesforce instance URL is unknown; reconnect Salesforce")
    if not customer.get("email") and not customer.get("phone"):
        raise pc.ConnectorError("Lead has neither email nor phone")
    headers = {"Authorization": f"Bearer {token}"}
    base = f"{instance}/services/data/{SALESFORCE_API_VERSION}"
    existing_id = None
    if customer.get("email"):
        found = await pc._call(
            client,
            "GET",
            f"{base}/query",
            headers=headers,
            params={
                "q": "SELECT Id FROM Lead WHERE Email = '"
                f"{_soql_quote(customer['email'])}' AND IsConverted = false LIMIT 1"
            },
        )
        records = found.json().get("records") or []
        existing_id = records[0]["Id"] if records else None
    fields = salesforce_lead_fields(customer)
    if existing_id:
        fields.pop("Company", None)  # don't overwrite an existing company with "Unknown"
        await pc._call(
            client, "PATCH", f"{base}/sobjects/Lead/{existing_id}", headers=headers, json=fields
        )
        return "Salesforce lead updated"
    await pc._call(client, "POST", f"{base}/sobjects/Lead", headers=headers, json=fields)
    return "Salesforce lead created"


_PUSHERS = {"hubspot": _hubspot, "zoho": _zoho, "salesforce": _salesforce}


async def connected_crms(db: AsyncSession, tenant_id: uuid.UUID) -> list[OAuthConnection]:
    rows = (
        await db.execute(
            select(OAuthConnection).where(
                OAuthConnection.tenant_id == tenant_id,
                OAuthConnection.provider.in_(CRM_PROVIDERS),
                OAuthConnection.status == OAuthConnectionStatus.CONNECTED.value,
            )
        )
    ).scalars().all()
    return list(rows)


async def push_lead(
    db: AsyncSession,
    lead: CrmLead,
    *,
    provider: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """Push to one provider (or all connected CRMs). Returns
    [{provider, ok, detail}]; raises OAuthError(not_connected) only when a
    specific provider was requested and is not connected."""
    if provider is not None:
        if provider not in CRM_PROVIDERS:
            raise OAuthError("unknown_provider", f"{provider} is not a CRM provider", status_code=404)
        conn = await get_connection(db, lead.tenant_id, provider)
        if conn is None:
            raise OAuthError("not_connected", f"{provider} is not connected", status_code=404)
        connections = [conn]
    else:
        connections = await connected_crms(db, lead.tenant_id)
    if not connections:
        return []
    customer = await _customer_dict(db, lead)
    own = client is None
    http = client or new_client(PUSH_TIMEOUT)
    results: list[dict[str, Any]] = []
    sync = dict((lead.custom_fields or {}).get("crm_sync") or {})
    try:
        for conn in connections:
            try:
                token = await get_valid_access_token(db, lead.tenant_id, conn.provider, client=http)
                detail = await _PUSHERS[conn.provider](http, token, customer, conn)
                ok = True
            except OAuthError as exc:
                ok, detail = False, exc.message
            except pc.ConnectorError as exc:
                ok, detail = False, str(exc)
            except httpx.HTTPError as exc:
                ok, detail = False, f"Request failed: {exc.__class__.__name__}"
            results.append({"provider": conn.provider, "ok": ok, "detail": detail})
            sync[conn.provider] = {
                "ok": ok,
                "detail": detail[:300],
                "at": datetime.now(UTC).isoformat(),
            }
    finally:
        if own:
            await http.aclose()
    lead.custom_fields = {**(lead.custom_fields or {}), "crm_sync": sync}
    await db.flush()
    return results


async def push_lead_safely(db: AsyncSession, lead: CrmLead) -> None:
    """Best effort hook for lead creation: never raises, never aborts the
    caller's transaction (the lookup + bookkeeping run in a SAVEPOINT)."""
    try:
        async with db.begin_nested():
            if not await connected_crms(db, lead.tenant_id):
                return
            await push_lead(db, lead)
    except Exception:
        logger.warning("CRM push failed for lead %s", getattr(lead, "id", "?"), exc_info=True)

