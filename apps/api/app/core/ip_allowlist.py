"""Per-tenant IP allowlist. Enforced from get_current_context (deps.py), so it
covers every authenticated route — JWT sessions and API keys alike."""

import ipaddress
import time
import uuid

from app.core.config import get_settings
from app.core.exceptions import PermissionDeniedError
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.platform import TenantSecuritySettings

_CACHE_TTL_SECONDS = 30.0
# tenant_id -> (expires_at, parsed networks). Process-local; writers call
# invalidate_allowlist_cache so the changing process sees edits at once and
# others within the TTL.
_cache: dict[uuid.UUID, tuple[float, list[str]]] = {}


def invalidate_allowlist_cache(tenant_id: uuid.UUID | None = None) -> None:
    if tenant_id is None:
        _cache.clear()
    else:
        _cache.pop(tenant_id, None)


def normalize_entry(entry: str) -> str:
    """Validate an IP or CIDR and return its canonical text form."""
    return str(ipaddress.ip_network(entry.strip(), strict=False))


def ip_allowed(ip: str, entries: list[str]) -> bool:
    if not entries:
        return True
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(address in ipaddress.ip_network(e, strict=False) for e in entries)


def client_ip(request: Request) -> str | None:
    """X-Forwarded-For is only honoured when TRUST_X_FORWARDED_FOR is set
    (i.e. we sit behind a proxy we control); otherwise it is client-spoofable."""
    if get_settings().trust_x_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


async def _entries_for(db: AsyncSession, tenant_id: uuid.UUID) -> list[str]:
    now = time.monotonic()
    hit = _cache.get(tenant_id)
    if hit is not None and hit[0] > now:
        return hit[1]
    row = (
        await db.execute(
            select(TenantSecuritySettings.ip_allowlist).where(
                TenantSecuritySettings.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    entries = list(row or [])
    _cache[tenant_id] = (now + _CACHE_TTL_SECONDS, entries)
    return entries


async def enforce_ip_allowlist(
    request: Request, db: AsyncSession, tenant_id: uuid.UUID, *, bypass: bool = False
) -> None:
    if bypass:
        return
    entries = await _entries_for(db, tenant_id)
    if not entries:
        return
    ip = client_ip(request)
    if ip is None or not ip_allowed(ip, entries):
        raise PermissionDeniedError("Your IP address is not on this workspace's allowlist")
