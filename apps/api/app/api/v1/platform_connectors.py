"""Integrations hub: connector catalog, CRUD, test, plus SDK discovery info."""

import uuid

import httpx
from app.core.audit import log_audit_event
from app.core.config import get_settings
from app.core.deps import RequestContext, get_db, require_permission
from app.core.exceptions import NotFoundError
from app.core.permissions import Permissions
from app.schemas.platform import (
    ConnectorCatalogEntry,
    ConnectorCreate,
    ConnectorRead,
    ConnectorTestResult,
    ConnectorUpdate,
    SdkInfo,
)
from app.services import platform_connectors as hub
from app.services.webhooks import WEBHOOK_EVENTS
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.platform import IntegrationConnector

router = APIRouter()


def _read(connector: IntegrationConnector) -> ConnectorRead:
    definition = hub.CATALOG[connector.type]
    config = hub.mask_config(definition, hub.decrypt_config(connector.config))
    return ConnectorRead(
        id=connector.id,
        type=connector.type,
        name=connector.name,
        enabled=connector.enabled,
        events=list(connector.events or []),
        config=config,
        last_test_at=connector.last_test_at,
        last_test_ok=connector.last_test_ok,
        last_error=connector.last_error,
        created_at=connector.created_at,
    )


def _check_events(events: list[str]) -> list[str]:
    unknown = [e for e in events if e not in WEBHOOK_EVENTS]
    if unknown:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown event(s): {', '.join(unknown)}")
    return sorted(set(events))


async def _get(
    db: AsyncSession, tenant_id: uuid.UUID, connector_id: uuid.UUID
) -> IntegrationConnector:
    connector = (
        await db.execute(
            select(IntegrationConnector).where(
                IntegrationConnector.id == connector_id, IntegrationConnector.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if connector is None:
        raise NotFoundError("Connector not found")
    return connector


@router.get("/connectors/catalog", response_model=list[ConnectorCatalogEntry])
async def connector_catalog(
    _: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_READ)),
) -> list[ConnectorCatalogEntry]:
    return hub.catalog_entries()


@router.get("/connectors", response_model=list[ConnectorRead])
async def list_connectors(
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[ConnectorRead]:
    rows = (
        (
            await db.execute(
                select(IntegrationConnector)
                .where(IntegrationConnector.tenant_id == ctx.tenant_id)
                .order_by(IntegrationConnector.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_read(c) for c in rows if c.type in hub.CATALOG]


@router.post("/connectors", response_model=ConnectorRead, status_code=status.HTTP_201_CREATED)
async def create_connector(
    payload: ConnectorCreate,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> ConnectorRead:
    definition = hub.CATALOG.get(payload.type)
    if definition is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown connector type {payload.type!r}")
    if not definition.supported:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, definition.unsupported_reason)
    if definition.oauth_provider and not definition.fields:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{definition.label} is connected with OAuth: POST /api/v1/oauth/"
            f"{definition.oauth_provider}/authorize",
        )
    try:
        config = hub.validate_config(definition, payload.config)
    except hub.ConnectorError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    connector = IntegrationConnector(
        tenant_id=ctx.tenant_id,
        type=payload.type,
        name=payload.name,
        enabled=payload.enabled,
        config=hub.encrypt_config(config),
        events=_check_events(payload.events or definition.default_events),
    )
    db.add(connector)
    await db.flush()
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="connector.created",
        resource_type="connector",
        resource_id=connector.id,
        extra={"type": payload.type},
    )
    await db.commit()
    await db.refresh(connector)
    return _read(connector)


@router.patch("/connectors/{connector_id}", response_model=ConnectorRead)
async def update_connector(
    connector_id: uuid.UUID,
    payload: ConnectorUpdate,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> ConnectorRead:
    connector = await _get(db, ctx.tenant_id, connector_id)
    definition = hub.CATALOG[connector.type]
    if payload.name is not None:
        connector.name = payload.name
    if payload.enabled is not None:
        connector.enabled = payload.enabled
    if payload.events is not None:
        connector.events = _check_events(payload.events)
    if payload.config is not None:
        merged = hub.decrypt_config(connector.config)
        for key, value in payload.config.items():
            # The UI echoes masked secrets back; blank/masked = keep stored value.
            if value in ("", hub.SECRET_MASK):
                continue
            merged[key] = value
        try:
            merged = hub.validate_config(definition, merged)
        except hub.ConnectorError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        connector.config = hub.encrypt_config(merged)
    await db.commit()
    await db.refresh(connector)
    return _read(connector)


@router.delete("/connectors/{connector_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connector(
    connector_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    connector = await _get(db, ctx.tenant_id, connector_id)
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="connector.deleted",
        resource_type="connector",
        resource_id=connector.id,
        extra={"type": connector.type},
    )
    await db.delete(connector)
    await db.commit()


@router.post("/connectors/{connector_id}/test", response_model=ConnectorTestResult)
async def test_connector(
    connector_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> ConnectorTestResult:
    connector = await _get(db, ctx.tenant_id, connector_id)
    async with httpx.AsyncClient(follow_redirects=False) as client:
        ok, detail = await hub.test_connector(connector, client)
    connector.last_test_at = hub.now_utc()
    connector.last_test_ok = ok
    connector.last_error = None if ok else detail[:1000]
    await db.commit()
    return ConnectorTestResult(ok=ok, detail=detail)


@router.get("/openapi-sdk-info", response_model=SdkInfo)
async def openapi_sdk_info(
    _: RequestContext = Depends(require_permission(Permissions.INTEGRATIONS_READ)),
) -> SdkInfo:
    base = get_settings().public_base_url.rstrip("/")
    return SdkInfo(
        openapi_url=f"{base}/openapi.json",
        api_base_url=f"{base}/api/v1",
        auth="Authorization: Bearer ak_live_... (create keys under Developers > API keys)",
        webhook_signature={
            "header": "X-Signature",
            "timestamp_header": "X-Timestamp",
            "algorithm": "hex(HMAC-SHA256(signing_secret, f'{timestamp}.{raw_body}'))",
        },
        sdks=[
            {"language": "javascript", "path": "sdk/javascript", "package": "autonxt"},
            {"language": "python", "path": "sdk/python", "package": "autonxt"},
            {"language": "php", "path": "sdk/php", "package": "autonxt/autonxt-php"},
        ],
    )
