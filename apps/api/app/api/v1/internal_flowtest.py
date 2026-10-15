"""Gateway-facing endpoints for browser voice tests (Flow Studio "Test Audio").

The realtime gateway runs a browser test exactly like a phone call — same
Sarvam STT/TTS, barge-in and silence handling — but calls these instead of
/internal/calls/*: each turn goes to the test engine with the unsaved canvas
and simulated tools, and nothing is written to Call/Conversation/Message.
Shapes match the live-call endpoints, plus the test timeline fields.
"""

import uuid

from app.core.deps import get_db
from app.core.internal_auth import require_internal_service
from app.integrations.credentials import get_sarvam_api_key
from app.integrations.sarvam import SarvamAPIError
from app.services import test_sessions
from app.services.test_engine import TestVariables, actions_from_events, opening_line, run_test_turn
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import AIAgent
from packages.database.session import set_tenant_context

router = APIRouter(prefix="/internal/test-calls", tags=["internal"], dependencies=[Depends(require_internal_service)])


async def _session(sid: str) -> dict:
    data = await test_sessions.load(sid)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Test session not found or expired")
    return data


async def _agent(db: AsyncSession, data: dict) -> AIAgent:
    await set_tenant_context(db, uuid.UUID(data["tenant_id"]))
    agent = await db.get(AIAgent, uuid.UUID(data["agent_id"]))
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    return agent


@router.get("/{sid}/config")
async def test_call_config(sid: str, token: str = "", db: AsyncSession = Depends(get_db)) -> dict:
    data = await _session(sid)
    # The browser's ticket: proves this socket belongs to whoever created the session.
    if not token or token != data.get("token"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Bad test session token")
    agent = await _agent(db, data)
    return {
        "sarvam_api_key": await get_sarvam_api_key(db, agent.tenant_id) or "",
        "language": agent.language or "en-IN",
        "voice": agent.voice or "",
        "greeting": opening_line(agent),
        "max_call_duration_seconds": agent.max_call_duration_seconds,
        "max_duration_message": agent.max_duration_message,
        "no_response_timeout_seconds": agent.no_response_timeout_seconds,
        "max_no_response_retries": agent.max_no_response_retries,
        "no_response_prompt": agent.no_response_prompt,
        "no_response_end_message": agent.no_response_end_message,
        "start_node_id": data.get("current_node_id"),
    }


class TurnIn(BaseModel):
    transcript: str


@router.post("/{sid}/turn")
async def test_call_turn(sid: str, payload: TurnIn, db: AsyncSession = Depends(get_db)) -> dict:
    data = await _session(sid)
    agent = await _agent(db, data)
    history = data.get("history") or []
    history.append({"role": "user", "content": payload.transcript})
    try:
        r = await run_test_turn(
            db=db,
            agent=agent,
            nodes=data.get("nodes") or [],
            edges=data.get("edges") or [],
            history=[{"role": m["role"], "content": m["content"]} for m in history],
            variables=TestVariables.from_dict(data.get("variables")),
            voice=True,
            start_node_id=data.get("start_node_id"),
            previous_node_id=data.get("current_node_id"),
            track=False,  # live-call speed; the browser asks for the node separately
            prior_actions=data.get("actions") or [],
        )
    except SarvamAPIError:
        r = None
    if r is None:
        reply = "Sorry, I'm having trouble right now."
        history.append({"role": "assistant", "content": reply, "id": uuid.uuid4().hex})
        data["history"] = history
        await test_sessions.save(sid, data)
        return {"reply_text": reply, "action": "none", "reason": None, "reply_message_id": history[-1]["id"], "events": [], "error": "model unavailable"}
    message_id = uuid.uuid4().hex
    history.append({"role": "assistant", "content": r.reply, "id": message_id})
    data["history"] = history
    data["current_node_id"] = r.node_id or data.get("current_node_id")
    data["actions"] = [*(data.get("actions") or []), *actions_from_events(r.events)][-20:]
    await test_sessions.save(sid, data)
    return {
        "reply_text": r.reply,
        "action": "transfer" if r.transfer else "none",
        "reason": "test transfer (simulated)" if r.transfer else None,
        "reply_message_id": message_id,
        # Test timeline, forwarded to the browser by the gateway.
        "node_id": r.node_id,
        "node_reason": r.node_reason,
        "events": r.events,
        "latency_ms": r.latency_ms,
        "usage": r.usage,
        "ended": r.ended,
    }


@router.delete("/{sid}/messages/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
async def test_call_retract(sid: str, message_id: str) -> None:
    """The caller talked over this reply: drop it so history matches what they heard."""
    data = await _session(sid)
    data["history"] = [m for m in data.get("history") or [] if m.get("id") != message_id]
    await test_sessions.save(sid, data)


class EndIn(BaseModel):
    status: str | None = None
    duration_seconds: int | None = None
    action: str | None = None
    reason: str | None = None


@router.post("/{sid}/end")
@router.post("/{sid}/finalize")
async def test_call_end(sid: str, payload: EndIn) -> dict:
    data = await test_sessions.load(sid)
    if data:
        data["ended"] = {"status": payload.status, "action": payload.action, "reason": payload.reason}
        await test_sessions.save(sid, data)
    return {"ok": True}
