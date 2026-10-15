"""Flow Studio testing: real-engine test turns, voice test sessions, and saved
scenario test cases with run history. Everything here runs the same engine as
live calls (app/services/test_engine.py) with simulated tools."""

import asyncio
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.integrations.credentials import get_sarvam_api_key
from app.integrations.sarvam import SarvamAPIError, chat_completion
from app.services import test_sessions
from app.services.scenario_runner import CHECK_TYPES, run_batch
from app.services.test_engine import (
    TestVariables,
    actions_from_events,
    opening_line,
    run_test_turn,
    track_step,
)
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import AgentTestCase, AgentTestRun, AIAgent, AIAgentVersion

router = APIRouter(tags=["agent-tests"])

_background: set[asyncio.Task] = set()


async def _agent(db: AsyncSession, tenant_id: uuid.UUID, agent_id: uuid.UUID) -> AIAgent:
    agent = await db.get(AIAgent, agent_id)
    if agent is None or agent.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    return agent


async def _flow(db: AsyncSession, agent: AIAgent, nodes: list[dict] | None, edges: list[dict] | None) -> tuple[list[dict], list[dict], int | None]:
    """The canvas sent by the client (unsaved edits), else the latest saved version."""
    if nodes:
        return nodes, edges or [], None
    latest = (
        await db.execute(select(AIAgentVersion).where(AIAgentVersion.agent_id == agent.id).order_by(AIAgentVersion.version.desc()).limit(1))
    ).scalar_one_or_none()
    config = (latest.configuration if latest else None) or {}
    return config.get("nodes", []), config.get("edges", []), latest.version if latest else None


# ---------- One test turn (Test Chat, and Test Audio's typed fallback) ----------
class TurnMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class TestTurnRequest(BaseModel):
    messages: list[TurnMessage] = Field(default_factory=list, max_length=60)
    nodes: list[dict] | None = None
    edges: list[dict] | None = None
    channel: Literal["voice", "chat"] = "voice"
    variables: dict = Field(default_factory=dict)
    start_node_id: str | None = None
    current_node_id: str | None = None
    # False = return the reply at live-call speed and track the node separately.
    track: bool = False
    # Tool calls from earlier turns ({name, arguments}), so the agent knows what it already did.
    prior_tools: list[dict] = Field(default_factory=list, max_length=60)


class TestTurnResponse(BaseModel):
    reply: str
    node_id: str | None
    node_reason: str = ""
    events: list[dict[str, Any]] = Field(default_factory=list)
    latency_ms: int = 0
    usage: dict = Field(default_factory=dict)
    transfer: bool = False
    ended: bool = False
    greeting: bool = False


@router.post("/ai-agents/{agent_id}/test-turn", response_model=TestTurnResponse)
async def test_turn(
    agent_id: uuid.UUID,
    payload: TestTurnRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> TestTurnResponse:
    """With no messages: the call's opening (the agent's greeting, as the gateway
    speaks it). Otherwise: the agent's reply to the last caller message."""
    agent = await _agent(db, ctx.tenant_id, agent_id)
    nodes, edges, _ = await _flow(db, agent, payload.nodes, payload.edges)
    start = payload.start_node_id or next((n.get("id") for n in nodes if n.get("type") == "start"), None)
    if not payload.messages:
        greeting = opening_line(agent)
        if greeting:
            return TestTurnResponse(reply=greeting, node_id=start, node_reason="Call opening (agent greeting)", greeting=True)
        return TestTurnResponse(reply="", node_id=start, node_reason="No greeting set — the caller speaks first", greeting=True)
    if payload.messages[-1].role != "user":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The last message must be the caller's")
    try:
        r = await run_test_turn(
            db=db,
            agent=agent,
            nodes=nodes,
            edges=edges,
            history=[m.model_dump() for m in payload.messages],
            variables=TestVariables.from_dict(payload.variables),
            voice=payload.channel == "voice",
            start_node_id=payload.start_node_id,
            previous_node_id=payload.current_node_id or start,
            track=payload.track,
            prior_actions=actions_from_events([{"type": "tool", **t} for t in payload.prior_tools if isinstance(t, dict)]),
        )
    except SarvamAPIError as exc:
        raise HTTPException(status.HTTP_424_FAILED_DEPENDENCY, str(exc)) from exc
    return TestTurnResponse(**r.__dict__)


class TrackRequest(BaseModel):
    messages: list[TurnMessage] = Field(min_length=1, max_length=60)
    nodes: list[dict] | None = None
    edges: list[dict] | None = None
    previous_node_id: str | None = None


@router.post("/ai-agents/{agent_id}/test-track")
async def test_track(
    agent_id: uuid.UUID,
    payload: TrackRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Which flow node the conversation is on (run after each reply, off the
    reply's critical path). Also reports whether that node is an end step."""
    agent = await _agent(db, ctx.tenant_id, agent_id)
    nodes, _edges, _ = await _flow(db, agent, payload.nodes, payload.edges)
    tracked = await track_step(nodes, [m.model_dump() for m in payload.messages], await get_sarvam_api_key(db, ctx.tenant_id), payload.previous_node_id)
    node = next((n for n in nodes if n.get("id") == tracked["node_id"]), None)
    return {**tracked, "ended": bool(node and node.get("type") == "end")}


# ---------- Voice test sessions (browser ⇄ realtime gateway ⇄ test engine) ----------
class TestSessionRequest(BaseModel):
    nodes: list[dict] | None = None
    edges: list[dict] | None = None
    variables: dict = Field(default_factory=dict)
    start_node_id: str | None = None


@router.post("/ai-agents/{agent_id}/test-sessions")
async def create_test_session(
    agent_id: uuid.UUID,
    payload: TestSessionRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    agent = await _agent(db, ctx.tenant_id, agent_id)
    nodes, edges, _ = await _flow(db, agent, payload.nodes, payload.edges)
    sid, token = await test_sessions.create(
        {
            "tenant_id": str(ctx.tenant_id),
            "agent_id": str(agent.id),
            "nodes": nodes,
            "edges": edges,
            "variables": payload.variables,
            "start_node_id": payload.start_node_id,
            "current_node_id": payload.start_node_id or next((n.get("id") for n in nodes if n.get("type") == "start"), None),
            # The gateway speaks the greeting itself; the agent must know it already did.
            "history": [{"role": "assistant", "content": g, "id": "greeting"}] if (g := opening_line(agent)) else [],
        }
    )
    return {"session_id": sid, "ws_url": test_sessions.browser_ws_url(sid, token), "token": token}


# ---------- Scenario test cases ----------
class Check(BaseModel):
    type: str
    value: str = Field(default="", max_length=1000)


class TestCaseIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    persona: str = Field(min_length=1, max_length=4000)
    opening: str = Field(default="", max_length=1000)
    variables: dict = Field(default_factory=dict)
    checks: list[Check] = Field(default_factory=list, max_length=20)
    max_turns: int = Field(default=8, ge=1, le=20)
    enabled: bool = True


def _case_out(c: AgentTestCase) -> dict:
    return {
        "id": str(c.id),
        "name": c.name,
        "persona": c.persona,
        "opening": c.opening,
        "variables": c.variables,
        "checks": c.checks,
        "max_turns": c.max_turns,
        "enabled": c.enabled,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def _clean_checks(checks: list[Check]) -> list[dict]:
    bad = [c.type for c in checks if c.type not in CHECK_TYPES]
    if bad:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown check type: {', '.join(bad)}")
    return [c.model_dump() for c in checks]


@router.get("/ai-agents/{agent_id}/test-cases")
async def list_test_cases(
    agent_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    await _agent(db, ctx.tenant_id, agent_id)
    rows = (await db.execute(select(AgentTestCase).where(AgentTestCase.agent_id == agent_id).order_by(AgentTestCase.created_at))).scalars().all()
    return [_case_out(c) for c in rows]


@router.post("/ai-agents/{agent_id}/test-cases", status_code=status.HTTP_201_CREATED)
async def create_test_case(
    agent_id: uuid.UUID,
    payload: TestCaseIn,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _agent(db, ctx.tenant_id, agent_id)
    case = AgentTestCase(tenant_id=ctx.tenant_id, agent_id=agent_id, **{**payload.model_dump(), "checks": _clean_checks(payload.checks)})
    db.add(case)
    await db.commit()
    await db.refresh(case)
    return _case_out(case)


@router.put("/ai-agents/{agent_id}/test-cases/{case_id}")
async def update_test_case(
    agent_id: uuid.UUID,
    case_id: uuid.UUID,
    payload: TestCaseIn,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    case = await db.get(AgentTestCase, case_id)
    if case is None or case.tenant_id != ctx.tenant_id or case.agent_id != agent_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Test case not found")
    for k, v in {**payload.model_dump(), "checks": _clean_checks(payload.checks)}.items():
        setattr(case, k, v)
    await db.commit()
    await db.refresh(case)
    return _case_out(case)


@router.delete("/ai-agents/{agent_id}/test-cases/{case_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_test_case(
    agent_id: uuid.UUID,
    case_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    case = await db.get(AgentTestCase, case_id)
    if case and case.tenant_id == ctx.tenant_id and case.agent_id == agent_id:
        await db.delete(case)
        await db.commit()


class SuggestRequest(BaseModel):
    nodes: list[dict] | None = None
    edges: list[dict] | None = None


_JSON_ARRAY = re.compile(r"\[.*\]", re.DOTALL)


@router.post("/ai-agents/{agent_id}/test-cases/suggest")
async def suggest_test_cases(
    agent_id: uuid.UUID,
    payload: SuggestRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Drafts test cases from the flow (not saved — the user reviews them first)."""
    agent = await _agent(db, ctx.tenant_id, agent_id)
    nodes, _edges, _ = await _flow(db, agent, payload.nodes, payload.edges)
    steps = "\n".join(f'- id="{n.get("id")}" ({n.get("type")}) {n.get("title")}: {(n.get("prompt") or "")[:300]}' for n in nodes)
    try:
        completion = await chat_completion(
            messages=[
                {
                    "role": "system",
                    "content": "You write test scenarios for a phone voice agent. Reply ONLY with a JSON array of 4 objects: "
                    '{"name": "...", "persona": "<who the caller is and what they want, 1-2 sentences>", "opening": "<their first line>", '
                    '"checks": [{"type": "must_say|must_not_say|must_reach_node|must_end|judge", "value": "<text, node id, or plain-English criterion>"}]}. '
                    "Cover the happy path, an off-topic or difficult caller, a caller who wants a human, and an edge case. "
                    "Use node ids from the flow for must_reach_node. Prefer judge checks for behaviour.",
                },
                {"role": "user", "content": f"Agent: {agent.name}\nFlow steps:\n{steps}"},
            ],
            api_key=await get_sarvam_api_key(db, ctx.tenant_id),
            temperature=0.4,
            max_tokens=8000,
            timeout=110.0,
        )
    except SarvamAPIError as exc:
        raise HTTPException(status.HTTP_424_FAILED_DEPENDENCY, str(exc)) from exc
    text = re.sub(r"<think>.*?</think>", "", completion["choices"][0]["message"].get("content") or "", flags=re.DOTALL | re.IGNORECASE)
    m = _JSON_ARRAY.search(text)
    try:
        items = json.loads(m.group(0)) if m else []
    except ValueError:
        items = []
    out = []
    for it in items[:6]:
        if not isinstance(it, dict) or not it.get("name") or not it.get("persona"):
            continue
        checks = [{"type": c.get("type"), "value": str(c.get("value") or "")} for c in it.get("checks") or [] if isinstance(c, dict) and c.get("type") in CHECK_TYPES]
        out.append({"name": str(it["name"])[:200], "persona": str(it["persona"])[:4000], "opening": str(it.get("opening") or "")[:1000], "variables": {}, "checks": checks[:8], "max_turns": 8})
    return out


# ---------- Runs ----------
class RunRequest(BaseModel):
    case_ids: list[uuid.UUID] | None = None  # None = every enabled case
    nodes: list[dict] | None = None
    edges: list[dict] | None = None


def _run_out(r: AgentTestRun) -> dict:
    return {
        "id": str(r.id),
        "case_id": str(r.case_id) if r.case_id else None,
        "case_name": r.case_name,
        "batch_id": str(r.batch_id) if r.batch_id else None,
        "flow_version": r.flow_version,
        "status": r.status,
        "passed": r.passed,
        "results": r.results,
        "transcript": r.transcript,
        "total_tokens": r.total_tokens,
        "duration_ms": r.duration_ms,
        "error": r.error,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.post("/ai-agents/{agent_id}/test-runs", status_code=status.HTTP_202_ACCEPTED)
async def start_test_runs(
    agent_id: uuid.UUID,
    payload: RunRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    agent = await _agent(db, ctx.tenant_id, agent_id)
    nodes, edges, version = await _flow(db, agent, payload.nodes, payload.edges)
    q = select(AgentTestCase).where(AgentTestCase.agent_id == agent_id)
    q = q.where(AgentTestCase.id.in_(payload.case_ids)) if payload.case_ids else q.where(AgentTestCase.enabled.is_(True))
    cases = (await db.execute(q.order_by(AgentTestCase.created_at))).scalars().all()
    if not cases:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "No test cases to run")
    batch_id = uuid.uuid4()
    runs = [AgentTestRun(tenant_id=ctx.tenant_id, agent_id=agent_id, case_id=c.id, case_name=c.name, batch_id=batch_id, flow_version=version) for c in cases]
    db.add_all(runs)
    await db.commit()
    task = asyncio.create_task(run_batch(ctx.tenant_id, agent_id, [r.id for r in runs], nodes, edges))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return {"batch_id": str(batch_id), "runs": [_run_out(r) for r in runs]}


@router.get("/ai-agents/{agent_id}/test-runs")
async def list_test_runs(
    agent_id: uuid.UUID,
    batch_id: uuid.UUID | None = None,
    limit: int = 50,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    await _agent(db, ctx.tenant_id, agent_id)
    q = select(AgentTestRun).where(AgentTestRun.agent_id == agent_id)
    if batch_id:
        q = q.where(AgentTestRun.batch_id == batch_id)
    rows = (await db.execute(q.order_by(desc(AgentTestRun.created_at)).limit(max(1, min(limit, 200))))).scalars().all()
    if _expire_stale_runs(rows):
        await db.commit()
    return [_run_out(r) for r in rows]


STALE_AFTER = timedelta(minutes=15)


def _expire_stale_runs(rows: list[AgentTestRun]) -> bool:
    """Batches run in a background task; a restart or deploy kills it mid-way. A batch
    with unfinished runs and no progress for STALE_AFTER is marked as interrupted."""
    now = datetime.now(timezone.utc)
    batches: dict[Any, list[AgentTestRun]] = {}
    for r in rows:
        batches.setdefault(r.batch_id or r.id, []).append(r)
    changed = False
    for runs in batches.values():
        pending = [r for r in runs if r.status in ("queued", "running")]
        if pending and all(now - r.updated_at > STALE_AFTER for r in runs):
            for r in pending:
                r.status = "error"
                r.passed = False
                r.error = "This run was interrupted (the server restarted). Run it again."
            changed = True
    return changed
