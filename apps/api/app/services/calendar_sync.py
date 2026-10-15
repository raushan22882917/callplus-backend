"""Mirror appointments into the tenant's connected Google / Outlook calendar
and (optionally) read their busy times back into availability.

Everything here is best effort: `sync_appointment` and `external_busy` never
raise, so a missing/expired/broken calendar connection can never break a
booking. DB reads run inside a SAVEPOINT so even a database error cannot abort
the caller's transaction."""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from app.integrations.oauth.errors import OAuthError
from app.integrations.oauth.http import new_client
from app.integrations.oauth.tokens import get_valid_access_token
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Customer
from packages.database.models.oauth import OAuthConnection, OAuthConnectionStatus
from packages.database.models.operations import Appointment, AppointmentResource, BookableService

logger = logging.getLogger(__name__)

CALENDAR_PROVIDERS = ("google", "microsoft")
SYNC_TIMEOUT = 6.0
GOOGLE = "https://www.googleapis.com/calendar/v3"
GRAPH = "https://graph.microsoft.com/v1.0"


async def calendar_connections(db: AsyncSession, tenant_id: uuid.UUID) -> list[OAuthConnection]:
    """Connected calendar providers for the tenant (empty on any error)."""
    try:
        async with db.begin_nested():
            rows = (
                await db.execute(
                    select(OAuthConnection).where(
                        OAuthConnection.tenant_id == tenant_id,
                        OAuthConnection.provider.in_(CALENDAR_PROVIDERS),
                        OAuthConnection.status == OAuthConnectionStatus.CONNECTED.value,
                    )
                )
            ).scalars().all()
            return list(rows)
    except Exception:
        logger.warning("calendar connection lookup failed", exc_info=True)
        return []


# ------------------------------------------------------------------ event bodies


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def google_event_body(
    appointment: Appointment, *, summary: str, description: str, tz_name: str
) -> dict[str, Any]:
    return {
        "summary": summary,
        "description": description,
        "start": {"dateTime": _iso(appointment.start_at) + "Z", "timeZone": tz_name},
        "end": {"dateTime": _iso(appointment.end_at) + "Z", "timeZone": tz_name},
        "extendedProperties": {"private": {"autonxt_appointment_id": str(appointment.id)}},
        "reminders": {"useDefault": True},
    }


def microsoft_event_body(
    appointment: Appointment, *, summary: str, description: str
) -> dict[str, Any]:
    return {
        "subject": summary,
        "body": {"contentType": "text", "content": description},
        "start": {"dateTime": _iso(appointment.start_at), "timeZone": "UTC"},
        "end": {"dateTime": _iso(appointment.end_at), "timeZone": "UTC"},
        "transactionId": str(appointment.id),
    }


async def _describe(db: AsyncSession, appointment: Appointment) -> tuple[str, str, str]:
    service = await db.get(BookableService, appointment.service_id)
    customer = await db.get(Customer, appointment.customer_id)
    resource = await db.get(AppointmentResource, appointment.resource_id)
    service_name = service.name if service else "Appointment"
    customer_name = (customer.name if customer else None) or "Customer"
    lines = [f"Customer: {customer_name}"]
    if customer and customer.phone:
        lines.append(f"Phone: {customer.phone}")
    if customer and customer.email:
        lines.append(f"Email: {customer.email}")
    if resource:
        lines.append(f"With: {resource.name}")
    if appointment.notes:
        lines.append(f"Notes: {appointment.notes}")
    lines.append("Booked via AutoNXT")
    return f"{service_name} - {customer_name}", "\n".join(lines), (
        resource.timezone if resource and resource.timezone else "UTC"
    )


# ------------------------------------------------------------------ providers


def _selected_calendar(conn: OAuthConnection) -> str | None:
    return ((conn.extra or {}).get("selected") or {}).get("calendar_id")


async def _google(
    client: httpx.AsyncClient,
    conn: OAuthConnection,
    token: str,
    action: str,
    appointment: Appointment,
    event_id: str | None,
    describe: tuple[str, str, str],
) -> str | None:
    """Returns the event id to remember (None = nothing to store)."""
    headers = {"Authorization": f"Bearer {token}"}
    calendar = quote(_selected_calendar(conn) or "primary", safe="")
    if action == "cancel":
        if event_id:
            response = await client.delete(
                f"{GOOGLE}/calendars/{calendar}/events/{quote(event_id, safe='')}",
                headers=headers,
                params={"sendUpdates": "none"},
            )
            if response.status_code not in (200, 204, 404, 410):
                response.raise_for_status()
        return None
    body = google_event_body(
        appointment, summary=describe[0], description=describe[1], tz_name=describe[2]
    )
    if event_id:
        response = await client.patch(
            f"{GOOGLE}/calendars/{calendar}/events/{quote(event_id, safe='')}",
            headers=headers,
            json=body,
            params={"sendUpdates": "none"},
        )
        if response.status_code in (404, 410):
            event_id = None  # deleted on their side: recreate
        else:
            response.raise_for_status()
            return event_id
    response = await client.post(
        f"{GOOGLE}/calendars/{calendar}/events",
        headers=headers,
        json=body,
        params={"sendUpdates": "none"},
    )
    response.raise_for_status()
    return response.json().get("id")


async def _microsoft(
    client: httpx.AsyncClient,
    conn: OAuthConnection,
    token: str,
    action: str,
    appointment: Appointment,
    event_id: str | None,
    describe: tuple[str, str, str],
) -> str | None:
    headers = {"Authorization": f"Bearer {token}", "Prefer": 'outlook.timezone="UTC"'}
    if action == "cancel":
        if event_id:
            response = await client.delete(
                f"{GRAPH}/me/events/{quote(event_id, safe='')}", headers=headers
            )
            if response.status_code not in (200, 204, 404, 410):
                response.raise_for_status()
        return None
    body = microsoft_event_body(appointment, summary=describe[0], description=describe[1])
    if event_id:
        response = await client.patch(
            f"{GRAPH}/me/events/{quote(event_id, safe='')}", headers=headers, json=body
        )
        if response.status_code in (404, 410):
            event_id = None
        else:
            response.raise_for_status()
            return event_id
    calendar = _selected_calendar(conn)
    url = f"{GRAPH}/me/calendars/{quote(calendar, safe='')}/events" if calendar else f"{GRAPH}/me/events"
    response = await client.post(url, headers=headers, json=body)
    response.raise_for_status()
    return response.json().get("id")


_HANDLERS = {"google": _google, "microsoft": _microsoft}


async def sync_appointment(
    db: AsyncSession,
    appointment: Appointment,
    action: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> None:
    """action: create | update | cancel. Never raises."""
    try:
        connections = await calendar_connections(db, appointment.tenant_id)
        if not connections:
            return
        describe = await _describe(db, appointment)
        events = dict(appointment.calendar_events or {})
        changed = False
        own = client is None
        http = client or new_client(SYNC_TIMEOUT)
        try:
            for conn in connections:
                try:
                    token = await get_valid_access_token(
                        db, appointment.tenant_id, conn.provider, client=http
                    )
                    new_id = await _HANDLERS[conn.provider](
                        http, conn, token, action, appointment, events.get(conn.provider), describe
                    )
                except OAuthError as exc:
                    logger.info("calendar %s skipped for %s: %s", conn.provider, appointment.id, exc.code)
                    continue
                except Exception:
                    logger.warning(
                        "calendar %s %s failed for appointment %s",
                        conn.provider,
                        action,
                        appointment.id,
                        exc_info=True,
                    )
                    continue
                if new_id and events.get(conn.provider) != new_id:
                    events[conn.provider] = new_id
                    changed = True
                elif not new_id and conn.provider in events:
                    events.pop(conn.provider)
                    changed = True
        finally:
            if own:
                await http.aclose()
        if changed:
            async with db.begin_nested():
                appointment.calendar_events = events
                await db.flush()
    except Exception:
        logger.warning("calendar sync crashed for appointment %s", getattr(appointment, "id", "?"), exc_info=True)


# ------------------------------------------------------------------ busy blocking


def _parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def _google_busy(
    client: httpx.AsyncClient, conn: OAuthConnection, token: str, start: datetime, end: datetime
) -> list[tuple[datetime, datetime]]:
    calendar = _selected_calendar(conn) or "primary"
    response = await client.post(
        f"{GOOGLE}/freeBusy",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "timeMin": start.astimezone(UTC).isoformat(),
            "timeMax": end.astimezone(UTC).isoformat(),
            "items": [{"id": calendar}],
        },
    )
    response.raise_for_status()
    busy = (response.json().get("calendars", {}).get(calendar) or {}).get("busy", [])
    return [(_parse_dt(b["start"]), _parse_dt(b["end"])) for b in busy]


async def _microsoft_busy(
    client: httpx.AsyncClient, conn: OAuthConnection, token: str, start: datetime, end: datetime
) -> list[tuple[datetime, datetime]]:
    calendar = _selected_calendar(conn)
    base = f"{GRAPH}/me/calendars/{quote(calendar, safe='')}" if calendar else f"{GRAPH}/me"
    response = await client.get(
        f"{base}/calendarView",
        headers={"Authorization": f"Bearer {token}", "Prefer": 'outlook.timezone="UTC"'},
        params={
            "startDateTime": start.astimezone(UTC).isoformat(),
            "endDateTime": end.astimezone(UTC).isoformat(),
            "$select": "start,end,showAs,isCancelled",
            "$top": "100",
        },
    )
    response.raise_for_status()
    out = []
    for item in response.json().get("value", []):
        if item.get("isCancelled") or item.get("showAs") == "free":
            continue
        out.append((_parse_dt(item["start"]["dateTime"]), _parse_dt(item["end"]["dateTime"])))
    return out


async def external_busy(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    start: datetime,
    end: datetime,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[tuple[datetime, datetime]]:
    """Busy intervals from calendars whose owner enabled `block_busy`. Never raises."""
    try:
        connections = [
            c
            for c in await calendar_connections(db, tenant_id)
            if ((c.extra or {}).get("selected") or {}).get("block_busy")
        ]
        if not connections:
            return []
        own = client is None
        http = client or new_client(SYNC_TIMEOUT)
        busy: list[tuple[datetime, datetime]] = []
        try:
            for conn in connections:
                try:
                    token = await get_valid_access_token(db, tenant_id, conn.provider, client=http)
                    fetch = _google_busy if conn.provider == "google" else _microsoft_busy
                    busy.extend(await fetch(http, conn, token, start, end))
                except Exception:
                    logger.warning("busy lookup failed for %s", conn.provider, exc_info=True)
        finally:
            if own:
                await http.aclose()
        return busy
    except Exception:
        logger.warning("external busy lookup crashed", exc_info=True)
        return []


def day_window(day, margin_days: int = 1) -> tuple[datetime, datetime]:
    base = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return base - timedelta(days=margin_days), base + timedelta(days=margin_days + 1)
