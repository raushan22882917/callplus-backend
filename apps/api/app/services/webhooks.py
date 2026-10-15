"""Outbound webhooks: tenant-registered HTTPS endpoints that receive signed
event payloads (call.ended, ticket.created, ...).

Delivery contract (documented in /docs): POST application/json
`{"id","event","created_at","data"}` with headers
  X-Event, X-Delivery-Id, X-Timestamp (unix seconds),
  X-Signature = hex(HMAC-SHA256(signing_secret, f"{timestamp}.{raw_body}")).
2xx = delivered; anything else is retried with exponential backoff (see
app/worker/webhook_tasks.py). Receivers should reject old timestamps.
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import time
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from app.core.crypto import decrypt_secret, encrypt_secret
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import WebhookDelivery, WebhookEndpoint

logger = logging.getLogger(__name__)

WEBHOOK_EVENTS = (
    "call.started",
    "call.ended",
    "call.transcript_ready",
    "ticket.created",
    "message.received",
    "campaign.completed",
    "sla.breached",
    "csat.received",
)
TEST_EVENT = "webhook.test"
MAX_ATTEMPTS = 6
REQUEST_TIMEOUT_SECONDS = 10.0


class UnsafeWebhookURL(ValueError):
    pass


def new_signing_secret() -> str:
    return f"whsec_{secrets.token_urlsafe(32)}"


def sign(secret: str, timestamp: int, body: str) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()


def assert_public_https_url(url: str) -> None:
    """Webhook URLs are fetched by our servers, so a tenant must not be able to
    point one at localhost, the cloud metadata service, or our own private
    network (SSRF). HTTPS only; every address the host resolves to must be
    public. Checked at registration AND again right before each delivery."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise UnsafeWebhookURL("Webhook URL must be https://")
    if parsed.username or parsed.password:
        raise UnsafeWebhookURL("Webhook URL must not contain credentials")
    port = parsed.port or 443
    try:
        infos = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeWebhookURL(f"Could not resolve {parsed.hostname}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global or address.is_multicast:
            raise UnsafeWebhookURL("Webhook URL must resolve to a public address")


def build_payload(delivery: WebhookDelivery) -> dict[str, Any]:
    return {
        "id": str(delivery.id),
        "event": delivery.event,
        "created_at": delivery.created_at.isoformat() if delivery.created_at else None,
        "data": delivery.payload,
    }


async def attempt_delivery(
    db: AsyncSession, delivery: WebhookDelivery, endpoint: WebhookEndpoint
) -> bool:
    """One HTTP attempt. Updates the delivery row (status, code, attempts,
    error) and flushes; the caller commits. Returns True on a 2xx."""
    delivery.attempts += 1
    body = json.dumps(build_payload(delivery), separators=(",", ":"), default=str)
    timestamp = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "CallPulse-Webhooks/1.0",
        "X-Event": delivery.event,
        "X-Delivery-Id": str(delivery.id),
        "X-Timestamp": str(timestamp),
        "X-Signature": sign(decrypt_secret(endpoint.signing_secret_encrypted), timestamp, body),
    }
    try:
        assert_public_https_url(endpoint.url)
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=False
        ) as client:
            response = await client.post(endpoint.url, content=body, headers=headers)
        delivery.response_code = response.status_code
        ok = 200 <= response.status_code < 300
        delivery.last_error = None if ok else f"HTTP {response.status_code}"
    except (httpx.HTTPError, UnsafeWebhookURL) as exc:
        ok = False
        delivery.response_code = None
        delivery.last_error = str(exc)[:500] or exc.__class__.__name__
    delivery.status = (
        "succeeded" if ok else ("failed" if delivery.attempts >= MAX_ATTEMPTS else "pending")
    )
    await db.flush()
    return ok


async def create_deliveries(
    db: AsyncSession, *, tenant_id: uuid.UUID, event: str, data: dict[str, Any]
) -> list[uuid.UUID]:
    """Fan one event out to every enabled endpoint subscribed to it."""
    result = await db.execute(
        select(WebhookEndpoint).where(
            WebhookEndpoint.tenant_id == tenant_id,
            WebhookEndpoint.enabled.is_(True),
            WebhookEndpoint.events.any(event),
        )
    )
    ids: list[uuid.UUID] = []
    for endpoint in result.scalars().all():
        delivery = WebhookDelivery(
            tenant_id=tenant_id,
            endpoint_id=endpoint.id,
            event=event,
            payload=data,
            created_at=datetime.now(UTC),
        )
        db.add(delivery)
        await db.flush()
        ids.append(delivery.id)
    return ids


def enqueue_webhook_event(*, tenant_id: uuid.UUID, event: str, data: dict[str, Any]) -> None:
    """Fire-and-forget from request handlers (same shape as
    enqueue_workflow_trigger): a slow or dead customer endpoint must never
    hold up a call turn. Never raises into the caller."""
    try:
        from app.worker.webhook_tasks import fan_out_event_task

        fan_out_event_task.delay(str(tenant_id), event, data)
    except Exception:
        logger.warning("could not enqueue webhook event %s", event, exc_info=True)


def encrypt_new_secret() -> tuple[str, str]:
    """Returns (plaintext, encrypted) for a fresh signing secret."""
    plaintext = new_signing_secret()
    return plaintext, encrypt_secret(plaintext)
