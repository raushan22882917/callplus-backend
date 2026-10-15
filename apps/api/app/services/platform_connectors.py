"""Outbound connector framework (integration_connectors).

Each connector type has: a field schema (drives the UI form), pure payload
builders (unit-tested), and `send`/`test` coroutines that perform the real
HTTP calls. Config is stored encrypted at rest (Fernet, app.core.crypto).

OAuth-backed types (google_calendar, outlook, salesforce, plus slack, hubspot,
zoho, shopify as the preferred path) are connected through the per-tenant OAuth
framework in app/integrations/oauth; their tokens live in `oauth_connections`.
"""

import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from app.core.crypto import decrypt_secret, encrypt_secret
from app.schemas.platform import ConnectorCatalogEntry, ConnectorFieldInfo
from app.services import oauth_notify
from app.services.webhooks import UnsafeWebhookURL, assert_public_https_url
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Customer
from packages.database.models.platform import IntegrationConnector

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 15.0
SECRET_MASK = "********"
SHOPIFY_API_VERSION = "2024-10"


class ConnectorError(Exception):
    pass


@dataclass
class ConnectorDef:
    type: str
    label: str
    category: str
    description: str
    fields: list[ConnectorFieldInfo]
    default_events: list[str]
    send: Callable[..., Awaitable[str]] | None = None
    test: Callable[..., Awaitable[str]] | None = None
    unsupported_reason: str | None = None
    # When set, the tenant connects this through the OAuth flow
    # (app/integrations/oauth, POST /oauth/{provider}/authorize) instead of
    # pasting credentials; `fields` then only holds the legacy manual fallback.
    oauth_provider: str | None = None
    extra_validation: Callable[[dict[str, str]], None] | None = None

    @property
    def supported(self) -> bool:
        return self.unsupported_reason is None

    @property
    def secret_keys(self) -> set[str]:
        return {f.key for f in self.fields if f.secret}


def _f(key: str, label: str, *, secret: bool = False, required: bool = True, ph: str | None = None):
    return ConnectorFieldInfo(
        key=key, label=label, secret=secret, required=required, placeholder=ph
    )


# ---- helpers ----------------------------------------------------------------


def summarize_event(event: str, data: dict[str, Any]) -> str:
    """Human-readable one-liner(s) for chat notifications."""
    lines = [f"Event: {event}"]
    for key in (
        "subject",
        "priority",
        "direction",
        "status",
        "duration_seconds",
        "from_number",
        "to_number",
        "call_id",
        "ticket_id",
        "text",
    ):
        if data.get(key) not in (None, ""):
            lines.append(f"{key.replace('_', ' ').capitalize()}: {data[key]}")
    return "\n".join(lines)


def split_name(name: str) -> tuple[str, str]:
    parts = name.strip().split(None, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else ("", parts[0] if parts else "Unknown")


def _public_url(url: str) -> str:
    try:
        assert_public_https_url(url)
    except UnsafeWebhookURL as exc:
        raise ConnectorError(str(exc)) from exc
    return url


def _slug(value: str, what: str) -> str:
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", value):
        raise ConnectorError(f"{what} must be a plain subdomain such as 'acme'")
    return value


async def _call(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
    try:
        response = await client.request(method, url, timeout=HTTP_TIMEOUT, **kwargs)
    except httpx.HTTPError as exc:
        raise ConnectorError(f"Request to {httpx.URL(url).host} failed: {exc}") from exc
    if response.status_code >= 400:
        raise ConnectorError(
            f"HTTP {response.status_code} from {httpx.URL(url).host}: {response.text[:300]}"
        )
    return response


# ---- chat notifications / automation hooks -----------------------------------


def slack_payload(event: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"text": f":telephone_receiver: {summarize_event(event, data)}"}


def teams_payload(event: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "summary": f"AutoNXT: {event}",
        "title": f"AutoNXT: {event}",
        "text": summarize_event(event, data).replace("\n", "  \n"),
    }


def automation_body(
    event: str, data: dict[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    return {
        "event": event,
        "data": data,
        "timestamp": int(now if now is not None else time.time()),
    }


def sign_body(secret: str, timestamp: int, body: str) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()


async def _post_json(
    client: httpx.AsyncClient, url: str, payload: dict[str, Any]
) -> httpx.Response:
    return await _call(client, "POST", _public_url(url), json=payload)


async def send_slack(config, event, data, customer, client) -> str:
    await _post_json(client, config["webhook_url"], slack_payload(event, data))
    return "Posted to Slack"


async def test_slack(config, client) -> str:
    await _post_json(client, config["webhook_url"], {"text": "AutoNXT connector test: it works."})
    return "Test message posted to Slack"


async def send_teams(config, event, data, customer, client) -> str:
    await _post_json(client, config["webhook_url"], teams_payload(event, data))
    return "Posted to Teams"


async def test_teams(config, client) -> str:
    payload = teams_payload("connector.test", {"text": "AutoNXT connector test: it works."})
    await _post_json(client, config["webhook_url"], payload)
    return "Test card posted to Teams"


def make_automation_sender(label: str):
    async def send(config, event, data, customer, client) -> str:
        body = json.dumps(automation_body(event, data), separators=(",", ":"), default=str)
        headers = {"Content-Type": "application/json", "X-Event": event}
        secret = config.get("signing_secret")
        if secret:
            ts = int(time.time())
            headers["X-Timestamp"] = str(ts)
            headers["X-Signature"] = sign_body(secret, ts, body)
        await _call(
            client, "POST", _public_url(config["webhook_url"]), content=body, headers=headers
        )
        return f"Event delivered to {label}"

    async def test(config, client) -> str:
        await send(config, "connector.test", {"text": "AutoNXT connector test"}, None, client)
        return f"Test event delivered to {label}"

    return send, test


# ---- HubSpot -----------------------------------------------------------------

HUBSPOT = "https://api.hubapi.com"


def hubspot_properties(customer: dict[str, Any]) -> dict[str, str]:
    first, last = split_name(customer.get("name") or "")
    props = {"firstname": first, "lastname": last}
    if customer.get("phone"):
        props["phone"] = customer["phone"]
    return {k: v for k, v in props.items() if v}


def hubspot_upsert_request(customer: dict[str, Any]) -> dict[str, Any]:
    """Contacts batch upsert keyed on email (HubSpot CRM v3)."""
    return {
        "inputs": [
            {
                "idProperty": "email",
                "id": customer["email"].lower(),
                "properties": hubspot_properties(customer),
            }
        ]
    }


async def send_hubspot(config, event, data, customer, client) -> str:
    if not customer:
        raise ConnectorError("No customer is attached to this event")
    headers = {"Authorization": f"Bearer {config['access_token']}"}
    if customer.get("email"):
        await _call(
            client,
            "POST",
            f"{HUBSPOT}/crm/v3/objects/contacts/batch/upsert",
            json=hubspot_upsert_request(customer),
            headers=headers,
        )
        return "HubSpot contact upserted by email"
    if not customer.get("phone"):
        raise ConnectorError("Customer has neither email nor phone to match a HubSpot contact")
    search = await _call(
        client,
        "POST",
        f"{HUBSPOT}/crm/v3/objects/contacts/search",
        headers=headers,
        json={
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "phone", "operator": "EQ", "value": customer["phone"]}
                    ]
                }
            ],
            "limit": 1,
        },
    )
    results = search.json().get("results", [])
    props = {"properties": hubspot_properties(customer)}
    if results:
        await _call(
            client,
            "PATCH",
            f"{HUBSPOT}/crm/v3/objects/contacts/{results[0]['id']}",
            headers=headers,
            json=props,
        )
        return "HubSpot contact updated (matched by phone)"
    await _call(client, "POST", f"{HUBSPOT}/crm/v3/objects/contacts", headers=headers, json=props)
    return "HubSpot contact created"


async def test_hubspot(config, client) -> str:
    await _call(
        client,
        "GET",
        f"{HUBSPOT}/crm/v3/objects/contacts",
        params={"limit": 1},
        headers={"Authorization": f"Bearer {config['access_token']}"},
    )
    return "HubSpot token accepted"


# ---- Zoho CRM ----------------------------------------------------------------

_ZOHO_ACCOUNTS = re.compile(r"^https://accounts\.zoho\.(com|eu|in|com\.au|jp|ca|sa|uk)$")
_ZOHO_API = re.compile(r"^https://www\.zohoapis\.(com|eu|in|com\.au|jp|ca|sa|uk)$")


def zoho_lead_request(customer: dict[str, Any]) -> dict[str, Any]:
    first, last = split_name(customer.get("name") or "")
    lead: dict[str, Any] = {"Last_Name": last, "Lead_Source": "AutoNXT"}
    if first:
        lead["First_Name"] = first
    if customer.get("email"):
        lead["Email"] = customer["email"]
    if customer.get("phone"):
        lead["Phone"] = customer["phone"]
    dup = ["Email"] if customer.get("email") else ["Phone"]
    return {"data": [lead], "duplicate_check_fields": dup}


def _zoho_domains(config: dict[str, str]) -> tuple[str, str]:
    accounts = config.get("accounts_domain") or "https://accounts.zoho.com"
    api = config.get("api_domain") or "https://www.zohoapis.com"
    if not _ZOHO_ACCOUNTS.match(accounts) or not _ZOHO_API.match(api):
        raise ConnectorError("Zoho domains must be official accounts.zoho.* / www.zohoapis.* hosts")
    return accounts, api


async def _zoho_token(config: dict[str, str], client: httpx.AsyncClient) -> str:
    accounts, _ = _zoho_domains(config)
    response = await _call(
        client,
        "POST",
        f"{accounts}/oauth/v2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": config["refresh_token"],
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
        },
    )
    token = response.json().get("access_token")
    if not token:
        raise ConnectorError(f"Zoho did not return an access token: {response.text[:200]}")
    return token


async def send_zoho(config, event, data, customer, client) -> str:
    if not customer:
        raise ConnectorError("No customer is attached to this event")
    if not customer.get("email") and not customer.get("phone"):
        raise ConnectorError("Customer has neither email nor phone")
    _, api = _zoho_domains(config)
    token = await _zoho_token(config, client)
    response = await _call(
        client,
        "POST",
        f"{api}/crm/v2/Leads/upsert",
        headers={"Authorization": f"Zoho-oauthtoken {token}"},
        json=zoho_lead_request(customer),
    )
    result = (response.json().get("data") or [{}])[0]
    if result.get("status") == "error":
        raise ConnectorError(f"Zoho rejected the lead: {result.get('message')}")
    return f"Zoho lead {result.get('action', 'saved')}"


async def test_zoho(config, client) -> str:
    await _zoho_token(config, client)
    return "Zoho refresh token accepted"


# ---- Freshdesk / Zendesk --------------------------------------------------------

FRESHDESK_PRIORITY = {"low": 1, "medium": 2, "high": 3, "urgent": 4}
ZENDESK_PRIORITY = {"low": "low", "medium": "normal", "high": "high", "urgent": "urgent"}


def freshdesk_ticket_request(
    data: dict[str, Any], customer: dict[str, Any] | None
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "subject": data.get("subject") or "Support request",
        "description": data.get("description") or data.get("subject") or "Created by AutoNXT",
        "status": 2,
        "priority": FRESHDESK_PRIORITY.get(str(data.get("priority", "medium")), 2),
    }
    customer = customer or {}
    if customer.get("email"):
        body["email"] = customer["email"]
    elif customer.get("phone"):
        body["phone"] = customer["phone"]
    else:
        raise ConnectorError("Freshdesk needs the customer's email or phone")
    if customer.get("name"):
        body["name"] = customer["name"]
    return body


def zendesk_ticket_request(data: dict[str, Any], customer: dict[str, Any] | None) -> dict[str, Any]:
    ticket: dict[str, Any] = {
        "subject": data.get("subject") or "Support request",
        "comment": {"body": data.get("description") or data.get("subject") or "Created by AutoNXT"},
        "priority": ZENDESK_PRIORITY.get(str(data.get("priority", "medium")), "normal"),
    }
    if customer and customer.get("email"):
        ticket["requester"] = {
            "name": customer.get("name") or customer["email"],
            "email": customer["email"],
        }
    return {"ticket": ticket}


async def send_freshdesk(config, event, data, customer, client) -> str:
    domain = _slug(config["domain"], "Freshdesk domain")
    response = await _call(
        client,
        "POST",
        f"https://{domain}.freshdesk.com/api/v2/tickets",
        auth=(config["api_key"], "X"),
        json=freshdesk_ticket_request(data, customer),
    )
    return f"Freshdesk ticket #{response.json().get('id')} created"


async def test_freshdesk(config, client) -> str:
    domain = _slug(config["domain"], "Freshdesk domain")
    await _call(
        client,
        "GET",
        f"https://{domain}.freshdesk.com/api/v2/tickets",
        params={"per_page": 1},
        auth=(config["api_key"], "X"),
    )
    return "Freshdesk API key accepted"


async def send_zendesk(config, event, data, customer, client) -> str:
    sub = _slug(config["subdomain"], "Zendesk subdomain")
    response = await _call(
        client,
        "POST",
        f"https://{sub}.zendesk.com/api/v2/tickets.json",
        auth=(f"{config['email']}/token", config["api_token"]),
        json=zendesk_ticket_request(data, customer),
    )
    return f"Zendesk ticket #{response.json().get('ticket', {}).get('id')} created"


async def test_zendesk(config, client) -> str:
    sub = _slug(config["subdomain"], "Zendesk subdomain")
    await _call(
        client,
        "GET",
        f"https://{sub}.zendesk.com/api/v2/users/me.json",
        auth=(f"{config['email']}/token", config["api_token"]),
    )
    return "Zendesk credentials accepted"


# ---- Shopify / WooCommerce (order lookup, used by the lookup_order tool) -------


def _shop_host(config: dict[str, str]) -> str:
    host = config["shop_domain"].strip().lower().removeprefix("https://").rstrip("/")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", host):
        raise ConnectorError("Shopify domain must look like your-store.myshopify.com")
    return host


def summarize_shopify_order(order: dict[str, Any]) -> dict[str, Any]:
    tracking = [
        {
            "company": f.get("tracking_company"),
            "number": f.get("tracking_number"),
            "url": f.get("tracking_url"),
            "status": f.get("shipment_status"),
        }
        for f in order.get("fulfillments", [])
    ]
    return {
        "platform": "shopify",
        "order_number": order.get("name"),
        "placed_at": order.get("created_at"),
        "payment_status": order.get("financial_status"),
        "fulfillment_status": order.get("fulfillment_status") or "unfulfilled",
        "total": order.get("total_price"),
        "currency": order.get("currency"),
        "items": [f"{i.get('quantity')} x {i.get('title')}" for i in order.get("line_items", [])],
        "tracking": tracking,
    }


def summarize_woo_order(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "platform": "woocommerce",
        "order_number": str(order.get("number") or order.get("id")),
        "placed_at": order.get("date_created"),
        "status": order.get("status"),
        "total": order.get("total"),
        "currency": order.get("currency"),
        "items": [f"{i.get('quantity')} x {i.get('name')}" for i in order.get("line_items", [])],
    }


def _norm_phone(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")[-10:]


def order_matches(
    order_email: str | None, order_phone: str | None, *, email: str | None, phone: str | None
) -> bool:
    """The caller must prove they own the order: matching email or phone."""
    if email and order_email and email.strip().lower() == order_email.strip().lower():
        return True
    return bool(
        phone
        and order_phone
        and _norm_phone(phone)
        and _norm_phone(phone) == _norm_phone(order_phone)
    )


async def lookup_shopify_order(
    config: dict[str, str], order_number: str, client: httpx.AsyncClient
) -> dict[str, Any] | None:
    number = order_number.strip().lstrip("#")
    response = await _call(
        client,
        "GET",
        f"https://{_shop_host(config)}/admin/api/{SHOPIFY_API_VERSION}/orders.json",
        params={"name": f"#{number}", "status": "any", "limit": 1},
        headers={"X-Shopify-Access-Token": config["access_token"]},
    )
    orders = response.json().get("orders", [])
    return orders[0] if orders else None


async def lookup_woo_order(
    config: dict[str, str], order_number: str, client: httpx.AsyncClient
) -> dict[str, Any] | None:
    site = _public_url(config["site_url"].rstrip("/"))
    number = order_number.strip().lstrip("#")
    if not number.isdigit():
        raise ConnectorError("WooCommerce order numbers are numeric")
    try:
        response = await _call(
            client,
            "GET",
            f"{site}/wp-json/wc/v3/orders/{number}",
            auth=(config["consumer_key"], config["consumer_secret"]),
        )
    except ConnectorError as exc:
        if "HTTP 404" in str(exc):
            return None
        raise
    return response.json()


async def test_shopify(config, client) -> str:
    await _call(
        client,
        "GET",
        f"https://{_shop_host(config)}/admin/api/{SHOPIFY_API_VERSION}/shop.json",
        headers={"X-Shopify-Access-Token": config["access_token"]},
    )
    return "Shopify token accepted"


async def test_woocommerce(config, client) -> str:
    site = _public_url(config["site_url"].rstrip("/"))
    await _call(
        client,
        "GET",
        f"{site}/wp-json/wc/v3/orders",
        params={"per_page": 1},
        auth=(config["consumer_key"], config["consumer_secret"]),
    )
    return "WooCommerce keys accepted"


# ---- catalog -------------------------------------------------------------------

_zap_send, _zap_test = make_automation_sender("Zapier")
_make_send, _make_test = make_automation_sender("Make")
_n8n_send, _n8n_test = make_automation_sender("n8n")

_HOOK_FIELDS = [
    _f("webhook_url", "Webhook URL", secret=True, ph="https://..."),
    _f("signing_secret", "Signing secret (optional)", secret=True, required=False),
]
ALL_EVENTS = [
    "call.started",
    "call.ended",
    "call.transcript_ready",
    "ticket.created",
    "message.received",
    "campaign.completed",
]

CATALOG: dict[str, ConnectorDef] = {
    d.type: d
    for d in [
        ConnectorDef(
            "slack",
            "Slack",
            "Notifications",
            "Post event alerts to a channel via an incoming webhook.",
            [
                _f(
                    "webhook_url",
                    "Incoming webhook URL",
                    secret=True,
                    ph="https://hooks.slack.com/services/...",
                )
            ],
            ["call.ended", "ticket.created"],
            send_slack,
            test_slack,
            oauth_provider="slack",
        ),
        ConnectorDef(
            "teams",
            "Microsoft Teams",
            "Notifications",
            "Post event alerts to a Teams channel via an incoming webhook.",
            [
                _f(
                    "webhook_url",
                    "Incoming webhook URL",
                    secret=True,
                    ph="https://...webhook.office.com/...",
                )
            ],
            ["call.ended", "ticket.created"],
            send_teams,
            test_teams,
        ),
        ConnectorDef(
            "zapier",
            "Zapier",
            "Automation",
            "POST every subscribed event to a Zapier Catch Hook.",
            _HOOK_FIELDS,
            ["call.ended"],
            _zap_send,
            _zap_test,
        ),
        ConnectorDef(
            "make",
            "Make",
            "Automation",
            "POST every subscribed event to a Make custom webhook.",
            _HOOK_FIELDS,
            ["call.ended"],
            _make_send,
            _make_test,
        ),
        ConnectorDef(
            "n8n",
            "n8n",
            "Automation",
            "POST every subscribed event to an n8n Webhook node.",
            _HOOK_FIELDS,
            ["call.ended"],
            _n8n_send,
            _n8n_test,
        ),
        ConnectorDef(
            "hubspot",
            "HubSpot",
            "CRM",
            "Upsert the customer as a HubSpot contact (CRM v3). Uses a private app access token.",
            [_f("access_token", "Private app access token", secret=True)],
            ["call.ended"],
            send_hubspot,
            test_hubspot,
            oauth_provider="hubspot",
        ),
        ConnectorDef(
            "zoho",
            "Zoho CRM",
            "CRM",
            "Create/update the customer as a Zoho CRM lead. Uses a self-client refresh token.",
            [
                _f("client_id", "Client ID"),
                _f("client_secret", "Client secret", secret=True),
                _f("refresh_token", "Refresh token", secret=True),
                _f(
                    "accounts_domain",
                    "Accounts domain",
                    required=False,
                    ph="https://accounts.zoho.com",
                ),
                _f("api_domain", "API domain", required=False, ph="https://www.zohoapis.com"),
            ],
            ["call.ended"],
            send_zoho,
            test_zoho,
            oauth_provider="zoho",
        ),
        ConnectorDef(
            "freshdesk",
            "Freshdesk",
            "Helpdesk",
            "Create a Freshdesk ticket when a ticket is created here.",
            [_f("domain", "Freshdesk subdomain", ph="acme"), _f("api_key", "API key", secret=True)],
            ["ticket.created"],
            send_freshdesk,
            test_freshdesk,
        ),
        ConnectorDef(
            "zendesk",
            "Zendesk",
            "Helpdesk",
            "Create a Zendesk ticket when a ticket is created here.",
            [
                _f("subdomain", "Zendesk subdomain", ph="acme"),
                _f("email", "Agent email"),
                _f("api_token", "API token", secret=True),
            ],
            ["ticket.created"],
            send_zendesk,
            test_zendesk,
        ),
        ConnectorDef(
            "shopify",
            "Shopify",
            "Commerce",
            "Lets AI agents look up order status (lookup_order tool).",
            [
                _f("shop_domain", "Store domain", ph="your-store.myshopify.com"),
                _f("access_token", "Admin API access token", secret=True),
            ],
            [],
            None,
            test_shopify,
            oauth_provider="shopify",
        ),
        ConnectorDef(
            "woocommerce",
            "WooCommerce",
            "Commerce",
            "Lets AI agents look up order status (lookup_order tool).",
            [
                _f("site_url", "Site URL", ph="https://shop.example.com"),
                _f("consumer_key", "Consumer key", secret=True),
                _f("consumer_secret", "Consumer secret", secret=True),
            ],
            [],
            None,
            test_woocommerce,
        ),
        ConnectorDef(
            "google_calendar",
            "Google Calendar",
            "Calendar",
            "Mirror booked, rescheduled and cancelled appointments to Google Calendar (OAuth).",
            [],
            [],
            oauth_provider="google",
        ),
        ConnectorDef(
            "outlook",
            "Outlook Calendar",
            "Calendar",
            "Mirror booked, rescheduled and cancelled appointments to Outlook Calendar (OAuth).",
            [],
            [],
            oauth_provider="microsoft",
        ),
        ConnectorDef(
            "salesforce",
            "Salesforce",
            "CRM",
            "Create or update Salesforce Leads when a CRM lead is created (OAuth).",
            [],
            [],
            oauth_provider="salesforce",
        ),
    ]
}


def catalog_entries() -> list[ConnectorCatalogEntry]:
    return [
        ConnectorCatalogEntry(
            type=d.type,
            label=d.label,
            category=d.category,
            description=d.description,
            supported=d.supported,
            unsupported_reason=d.unsupported_reason,
            oauth_provider=d.oauth_provider,
            fields=d.fields,
            default_events=d.default_events,
        )
        for d in CATALOG.values()
    ]


# ---- config storage ------------------------------------------------------------


def encrypt_config(config: dict[str, str]) -> dict[str, str]:
    return {"enc": encrypt_secret(json.dumps(config))}


def decrypt_config(stored: dict[str, Any]) -> dict[str, str]:
    return json.loads(decrypt_secret(stored["enc"]))


def mask_config(definition: ConnectorDef, config: dict[str, str]) -> dict[str, str]:
    return {k: (SECRET_MASK if k in definition.secret_keys and v else v) for k, v in config.items()}


def validate_config(definition: ConnectorDef, config: dict[str, str]) -> dict[str, str]:
    allowed = {f.key for f in definition.fields}
    unknown = set(config) - allowed
    if unknown:
        raise ConnectorError(f"Unknown config field(s): {', '.join(sorted(unknown))}")
    missing = [
        f.label for f in definition.fields if f.required and not config.get(f.key, "").strip()
    ]
    if missing:
        raise ConnectorError(f"Missing required field(s): {', '.join(missing)}")
    return {k: v.strip() for k, v in config.items() if v and v.strip()}


# ---- execution -------------------------------------------------------------------


def _customer_dict(customer: Customer | None) -> dict[str, Any] | None:
    if customer is None:
        return None
    return {
        "id": str(customer.id),
        "name": customer.name,
        "email": customer.email,
        "phone": customer.phone,
    }


async def run_connector(
    connector: IntegrationConnector,
    event: str,
    data: dict[str, Any],
    customer: dict[str, Any] | None,
    client: httpx.AsyncClient,
) -> tuple[bool, str]:
    definition = CATALOG.get(connector.type)
    if definition is None or definition.send is None:
        return False, "This connector does not react to events"
    try:
        detail = await definition.send(
            decrypt_config(connector.config), event, data, customer, client
        )
        return True, detail
    except ConnectorError as exc:
        return False, str(exc)


async def test_connector(
    connector: IntegrationConnector, client: httpx.AsyncClient
) -> tuple[bool, str]:
    definition = CATALOG.get(connector.type)
    if definition is None or definition.test is None:
        return False, definition.unsupported_reason if definition else "Unknown connector type"
    try:
        return True, await definition.test(decrypt_config(connector.config), client)
    except ConnectorError as exc:
        return False, str(exc)


async def dispatch_event(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    event: str,
    data: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    connectors = [
        c
        for c in (
            await db.execute(
                select(IntegrationConnector).where(
                    IntegrationConnector.tenant_id == tenant_id,
                    IntegrationConnector.enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
        if event in (c.events or [])
    ]
    slack_channel = await oauth_notify.slack_target(db, tenant_id, event)
    if not connectors and slack_channel is None:
        return []
    customer = None
    if data.get("customer_id"):
        try:
            customer = _customer_dict(
                (
                    await db.execute(
                        select(Customer).where(
                            Customer.id == uuid.UUID(str(data["customer_id"])),
                            Customer.tenant_id == tenant_id,
                        )
                    )
                ).scalar_one_or_none()
            )
        except ValueError:
            customer = None
    own_client = client is None
    client = client or httpx.AsyncClient(follow_redirects=False)
    results: list[dict[str, Any]] = []
    try:
        for connector in connectors:
            try:
                ok, detail = await run_connector(connector, event, data, customer, client)
            except Exception as exc:  # one broken connector must not stop the rest
                logger.exception("connector %s crashed", connector.id)
                ok, detail = False, f"Unexpected error: {exc.__class__.__name__}"
            connector.last_error = None if ok else detail[:1000]
            results.append(
                {
                    "connector_id": str(connector.id),
                    "type": connector.type,
                    "ok": ok,
                    "detail": detail,
                }
            )
        if slack_channel is not None:
            try:
                results.append(
                    await oauth_notify.post_slack(db, tenant_id, slack_channel, event, data)
                )
            except Exception:  # an OAuth notifier must never break other connectors
                logger.exception("slack oauth notification crashed for tenant %s", tenant_id)
    finally:
        if own_client:
            await client.aclose()
    return results


def now_utc() -> datetime:
    return datetime.now(UTC)


# ---- OAuth bridge: Shopify -------------------------------------------------------

OAUTH_SHOPIFY_NAME = "Shopify (OAuth)"


async def sync_shopify_connector(db: AsyncSession, conn) -> None:
    """Mirror the tenant's Shopify OAuth connection into an integration
    connector so the existing lookup_order tool works unchanged."""
    shop = (conn.extra or {}).get("shop")
    if not shop:
        return
    config = encrypt_config(
        {"shop_domain": shop, "access_token": decrypt_secret(conn.encrypted_access_token)}
    )
    existing = (
        await db.execute(
            select(IntegrationConnector).where(
                IntegrationConnector.tenant_id == conn.tenant_id,
                IntegrationConnector.type == "shopify",
                IntegrationConnector.name == OAUTH_SHOPIFY_NAME,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            IntegrationConnector(
                tenant_id=conn.tenant_id,
                type="shopify",
                name=OAUTH_SHOPIFY_NAME,
                enabled=True,
                config=config,
                events=[],
            )
        )
    else:
        existing.config = config
        existing.enabled = True
    await db.flush()


async def remove_shopify_connector(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    rows = (
        await db.execute(
            select(IntegrationConnector).where(
                IntegrationConnector.tenant_id == tenant_id,
                IntegrationConnector.type == "shopify",
                IntegrationConnector.name == OAUTH_SHOPIFY_NAME,
            )
        )
    ).scalars().all()
    for row in rows:
        await db.delete(row)
    await db.flush()
