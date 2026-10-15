"""Browser voice-test sessions: the unsaved canvas + test setup + running history
for one Test Audio call, kept in Redis for an hour. The realtime gateway drives
them through app/api/v1/internal_test.py exactly like a phone call, but every
turn goes to the test engine (simulated tools, nothing persisted)."""

import json
import secrets
import uuid

from app.core.config import get_settings
from redis.asyncio import Redis, from_url

TTL_SECONDS = 3600
PREFIX = "flowtest:"

_client: Redis | None = None


def _redis() -> Redis:
    global _client
    if _client is None:
        _client = from_url(get_settings().redis_url, decode_responses=True)
    return _client


def new_session_id() -> str:
    return "test_" + uuid.uuid4().hex


async def create(data: dict) -> tuple[str, str]:
    sid = new_session_id()
    token = secrets.token_urlsafe(24)
    await _redis().set(PREFIX + sid, json.dumps({"history": [], **data, "token": token, "turns": []}), ex=TTL_SECONDS)
    return sid, token


async def load(sid: str) -> dict | None:
    if not sid.startswith("test_"):
        return None
    raw = await _redis().get(PREFIX + sid)
    return json.loads(raw) if raw else None


async def save(sid: str, data: dict) -> None:
    await _redis().set(PREFIX + sid, json.dumps(data), ex=TTL_SECONDS)


async def delete(sid: str) -> None:
    await _redis().delete(PREFIX + sid)


def browser_ws_url(sid: str, token: str) -> str:
    """The gateway's browser-test endpoint, next to its Twilio one."""
    base = get_settings().realtime_ws_url
    base = base.rsplit("/ws/", 1)[0] if "/ws/" in base else base.rstrip("/")
    return f"{base}/ws/browser-test?session={sid}&token={token}"
