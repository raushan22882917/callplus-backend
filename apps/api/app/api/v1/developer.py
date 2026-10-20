"""Developer platform: API keys, outbound webhooks and usage for the
public API (see docs/backend-requirements.md in the frontend repo).

Key management is JWT-only (a key must not mint keys). Webhook endpoints can
also be managed by an API key carrying the `webhooks:manage` scope.
"""

import uuid
from datetime import UTC, datetime, timedelta

from app.core.api_keys import generate_developer_key
from app.core.audit import log_audit_event
from app.core.config import get_settings
from app.core.deps import RequestContext, get_current_context, get_db, require_permission
from app.core.exceptions import PermissionDeniedError
from app.core.permissions import Permissions
from app.core.repository import TenantScopedRepository
from app.schemas.developer import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyRead,
    ApiKeyRotate,
    ApiKeyUpdate,
    UsageDay,
    UsageQuota,
    UsageSummary,
    WebhookDeliveryRead,
    WebhookEndpointCreate,
    WebhookEndpointCreated,
    WebhookEndpointRead,
    WebhookEndpointUpdate,
)
from app.services.security_alerts import notify_owners
from app.services.webhooks import (
    TEST_EVENT,
    UnsafeWebhookURL,
    assert_public_https_url,
    attempt_delivery,
    encrypt_new_secret,
)
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    ApiKey,
    ApiKeyUsageDaily,
    Call,
    Conversation,
    ConversationChannel,
    Message,
    MessageSender,
    WebhookDelivery,
    WebhookEndpoint,
)

router = APIRouter(prefix="/developer", tags=["developer"])


class ApiKeyRepository(TenantScopedRepository[ApiKey]):
    model = ApiKey


class WebhookEndpointRepository(TenantScopedRepository[WebhookEndpoint]):
    model = WebhookEndpoint


def _key_read(key: ApiKey) -> dict:
    return {
        "id": key.id,
        "name": key.name,
        "prefix": key.key_prefix,
        "mode": key.mode,
        "scopes": list(key.scopes),
        "allowed_ips": list(key.allowed_ips or []),
        "last_used_at": key.last_used_at,
        "expires_at": key.expires_at,
        "revoked_at": key.revoked_at,
        "created_at": key.created_at,
    }


async def require_webhook_access(
    ctx: RequestContext = Depends(get_current_context),
) -> RequestContext:
    if ctx.api_key_id is not None:
        if "webhooks:manage" not in ctx.api_key_scopes:
            raise PermissionDeniedError("API key lacks the webhooks:manage scope")
        return ctx
    if not ctx.has_permission(Permissions.INTEGRATIONS_MANAGE):
        raise PermissionDeniedError(f"Missing permission(s): {Permissions.INTEGRATIONS_MANAGE}")
    return ctx


def _check_url(url: str) -> None:
    try:
        assert_public_https_url(url)
    except UnsafeWebhookURL as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


# --- API keys ---------------------------------------------------------------


@router.get("/api-keys", response_model=list[ApiKeyRead])
async def list_api_keys(
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    if ctx.api_key_id is not None:
        raise PermissionDeniedError("API keys cannot manage API keys")
    keys = await ApiKeyRepository(db, ctx.tenant_id).list(limit=200)
    return [_key_read(k) for k in keys]


@router.post("/api-keys", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    payload: ApiKeyCreate,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if ctx.api_key_id is not None:
        raise PermissionDeniedError("API keys cannot manage API keys")
    if payload.expires_at is not None and payload.expires_at <= datetime.now(UTC):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "expires_at must be in the future")

    secret, prefix, hashed = generate_developer_key(payload.mode)
    key = await ApiKeyRepository(db, ctx.tenant_id).create(
        name=payload.name,
        key_prefix=prefix,
        hashed_key=hashed,
        mode=payload.mode,
        scopes=sorted(set(payload.scopes)),
        expires_at=payload.expires_at,
        allowed_ips=payload.allowed_ips,
        created_by_user_id=ctx.user_id,
    )
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="api_key.created",
        resource_type="api_key",
        resource_id=key.id,
        extra={"mode": payload.mode, "scopes": key.scopes},
    )
    await db.commit()
    await db.refresh(key)
    if payload.mode == "live":
        await notify_owners(
            db,
            ctx.tenant_id,
            subject="A new live API key was created",
            message=f'A live API key named "{key.name}" was created with access to: '
            f"{', '.join(key.scopes)}.",
        )
    return {**_key_read(key), "secret": secret}


@router.patch("/api-keys/{key_id}", response_model=ApiKeyRead)
async def update_api_key(
    key_id: uuid.UUID,
    payload: ApiKeyUpdate,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if ctx.api_key_id is not None:
        raise PermissionDeniedError("API keys cannot manage API keys")
    key = await ApiKeyRepository(db, ctx.tenant_id).get(key_id)
    if key.revoked_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A revoked key cannot be edited")
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("expires_at") is not None and changes["expires_at"] <= datetime.now(UTC):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "expires_at must be in the future")
    if changes.get("name") is None:
        changes.pop("name", None)
    if changes.get("allowed_ips") is None:
        changes.pop("allowed_ips", None)
    for field, value in changes.items():
        setattr(key, field, value)
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="api_key.updated",
        resource_type="api_key",
        resource_id=key.id,
        extra={"fields": sorted(changes)},
    )
    await db.commit()
    await db.refresh(key)
    return _key_read(key)


@router.post(
    "/api-keys/{key_id}/rotate", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED
)
async def rotate_api_key(
    key_id: uuid.UUID,
    payload: ApiKeyRotate,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Issue a replacement with the same name, scopes and IP limits. The old
    key stops working after `grace_hours` (0 = immediately), so a deployment
    can switch over without downtime."""
    if ctx.api_key_id is not None:
        raise PermissionDeniedError("API keys cannot manage API keys")
    old = await ApiKeyRepository(db, ctx.tenant_id).get(key_id)
    now = datetime.now(UTC)
    if old.revoked_at is not None or (old.expires_at is not None and old.expires_at <= now):
        raise HTTPException(status.HTTP_409_CONFLICT, "Only a working key can be rotated")

    secret, prefix, hashed = generate_developer_key(old.mode)
    new = await ApiKeyRepository(db, ctx.tenant_id).create(
        name=old.name,
        key_prefix=prefix,
        hashed_key=hashed,
        mode=old.mode,
        scopes=list(old.scopes),
        expires_at=None,
        allowed_ips=list(old.allowed_ips or []),
        created_by_user_id=ctx.user_id,
    )
    if payload.grace_hours == 0:
        old.revoked_at = now
    else:
        ends = now + timedelta(hours=payload.grace_hours)
        # Never extend a key that was due to expire sooner than the grace period.
        old.expires_at = min(ends, old.expires_at) if old.expires_at else ends
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="api_key.rotated",
        resource_type="api_key",
        resource_id=old.id,
        extra={"new_key_id": str(new.id), "grace_hours": payload.grace_hours},
    )
    await db.commit()
    await db.refresh(new)
    if old.mode == "live":
        await notify_owners(
            db,
            ctx.tenant_id,
            subject="An API key was rotated",
            message=f'The live API key "{old.name}" was rotated. '
            + ("The old key stopped working immediately." if payload.grace_hours == 0 else f"The old key works for {payload.grace_hours} more hours."),
        )
    return {**_key_read(new), "secret": secret}


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    key_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    if ctx.api_key_id is not None:
        raise PermissionDeniedError("API keys cannot manage API keys")
    key = await ApiKeyRepository(db, ctx.tenant_id).get(key_id)
    if key.revoked_at is None:
        key.revoked_at = datetime.now(UTC)
        await log_audit_event(
            db,
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user_id,
            action="api_key.revoked",
            resource_type="api_key",
            resource_id=key.id,
        )
    await db.commit()


# --- Webhooks ---------------------------------------------------------------


@router.get("/webhooks", response_model=list[WebhookEndpointRead])
async def list_webhooks(
    ctx: RequestContext = Depends(require_webhook_access), db: AsyncSession = Depends(get_db)
) -> list[WebhookEndpoint]:
    return await WebhookEndpointRepository(db, ctx.tenant_id).list(limit=200)


@router.post(
    "/webhooks", response_model=WebhookEndpointCreated, status_code=status.HTTP_201_CREATED
)
async def create_webhook(
    payload: WebhookEndpointCreate,
    ctx: RequestContext = Depends(require_webhook_access),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _check_url(payload.url)
    secret, encrypted = encrypt_new_secret()
    endpoint = await WebhookEndpointRepository(db, ctx.tenant_id).create(
        url=payload.url, events=payload.events, enabled=True, signing_secret_encrypted=encrypted
    )
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="webhook.created",
        resource_type="webhook_endpoint",
        resource_id=endpoint.id,
        extra={"url": payload.url, "events": payload.events},
    )
    await db.commit()
    await db.refresh(endpoint)
    return {
        "id": endpoint.id,
        "url": endpoint.url,
        "events": list(endpoint.events),
        "enabled": endpoint.enabled,
        "created_at": endpoint.created_at,
        "signing_secret": secret,
    }


# Registered before /webhooks/{id} so "deliveries" is never parsed as an id.
@router.get("/webhooks/deliveries", response_model=list[WebhookDeliveryRead])
async def list_deliveries(
    limit: int = Query(default=50, ge=1, le=200),
    ctx: RequestContext = Depends(require_webhook_access),
    db: AsyncSession = Depends(get_db),
) -> list[WebhookDelivery]:
    result = await db.execute(
        select(WebhookDelivery)
        .where(WebhookDelivery.tenant_id == ctx.tenant_id)
        .order_by(WebhookDelivery.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


@router.patch("/webhooks/{endpoint_id}", response_model=WebhookEndpointRead)
async def update_webhook(
    endpoint_id: uuid.UUID,
    payload: WebhookEndpointUpdate,
    ctx: RequestContext = Depends(require_webhook_access),
    db: AsyncSession = Depends(get_db),
) -> WebhookEndpoint:
    endpoint = await WebhookEndpointRepository(db, ctx.tenant_id).get(endpoint_id)
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("url") is not None:
        _check_url(changes["url"])
    for field, value in changes.items():
        if value is not None:
            setattr(endpoint, field, sorted(set(value)) if field == "events" else value)
    await db.commit()
    await db.refresh(endpoint)
    return endpoint


@router.delete("/webhooks/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    endpoint_id: uuid.UUID,
    ctx: RequestContext = Depends(require_webhook_access),
    db: AsyncSession = Depends(get_db),
) -> None:
    await WebhookEndpointRepository(db, ctx.tenant_id).delete(endpoint_id)
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="webhook.deleted",
        resource_type="webhook_endpoint",
        resource_id=endpoint_id,
    )
    await db.commit()


@router.post("/webhooks/{endpoint_id}/test", response_model=WebhookDeliveryRead)
async def test_webhook(
    endpoint_id: uuid.UUID,
    ctx: RequestContext = Depends(require_webhook_access),
    db: AsyncSession = Depends(get_db),
) -> WebhookDelivery:
    """Sends one signed sample event right now (no retries) so the owner can
    verify their receiver and signature check."""
    endpoint = await WebhookEndpointRepository(db, ctx.tenant_id).get(endpoint_id)
    delivery = WebhookDelivery(
        tenant_id=ctx.tenant_id,
        endpoint_id=endpoint.id,
        event=TEST_EVENT,
        payload={"message": "This is a test event from your CallPulse workspace."},
        created_at=datetime.now(UTC),
    )
    db.add(delivery)
    await db.flush()
    await attempt_delivery(db, delivery, endpoint)
    # A test is a single attempt; don't leave it "pending" for a retry nobody queued.
    if delivery.status == "pending":
        delivery.status = "failed"
    await db.commit()
    await db.refresh(delivery)
    return delivery


# --- Usage ------------------------------------------------------------------


@router.get("/usage", response_model=UsageSummary)
async def get_usage(
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> UsageSummary:
    """Current calendar month (UTC). Call/STT minutes come from completed call
    durations; TTS characters from AI replies on voice calls; API requests from
    the per-key daily counters."""
    settings = get_settings()
    now = datetime.now(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    call_seconds = (
        await db.execute(
            select(func.coalesce(func.sum(Call.duration_seconds), 0)).where(
                Call.tenant_id == ctx.tenant_id, Call.created_at >= start
            )
        )
    ).scalar_one()
    call_minutes = round(call_seconds / 60, 2)

    tts_chars = (
        await db.execute(
            select(func.coalesce(func.sum(func.length(Message.text)), 0))
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.tenant_id == ctx.tenant_id,
                Message.created_at >= start,
                Message.sender == MessageSender.AI_AGENT,
                Conversation.channel == ConversationChannel.VOICE,
            )
        )
    ).scalar_one()

    whatsapp_messages = (
        await db.execute(
            select(func.count(Message.id))
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.tenant_id == ctx.tenant_id,
                Message.created_at >= start,
                Conversation.channel == ConversationChannel.WHATSAPP,
            )
        )
    ).scalar_one()

    usage_rows = (
        await db.execute(
            select(ApiKeyUsageDaily.day, func.sum(ApiKeyUsageDaily.requests))
            .where(
                ApiKeyUsageDaily.tenant_id == ctx.tenant_id, ApiKeyUsageDaily.day >= start.date()
            )
            .group_by(ApiKeyUsageDaily.day)
            .order_by(ApiKeyUsageDaily.day)
        )
    ).all()
    api_by_day = {day: int(total) for day, total in usage_rows}

    call_rows = (
        await db.execute(
            select(func.date(Call.created_at), func.coalesce(func.sum(Call.duration_seconds), 0))
            .where(Call.tenant_id == ctx.tenant_id, Call.created_at >= start)
            .group_by(func.date(Call.created_at))
        )
    ).all()
    minutes_by_day = {day: round(secs / 60, 2) for day, secs in call_rows}

    days = sorted(set(api_by_day) | set(minutes_by_day))
    daily = [
        UsageDay(day=d, call_minutes=minutes_by_day.get(d, 0.0), api_requests=api_by_day.get(d, 0))
        for d in days
    ]

    estimated = int(
        call_minutes * settings.usage_paise_per_call_minute
        + whatsapp_messages * settings.usage_paise_per_whatsapp_message
    )
    return UsageSummary(
        period_start=start,
        period_end=now,
        call_minutes=call_minutes,
        stt_minutes=call_minutes,
        tts_characters=int(tts_chars),
        whatsapp_messages=int(whatsapp_messages),
        api_requests=sum(api_by_day.values()),
        estimated_cost_paise=estimated,
        quota=UsageQuota(
            call_minutes=None,
            api_requests_per_minute=settings.api_key_rate_limit_per_minute,
            concurrent_calls=settings.max_concurrent_calls_per_tenant,
        ),
        daily=daily,
    )
