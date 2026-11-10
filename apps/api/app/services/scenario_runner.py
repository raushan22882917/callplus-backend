"""Scenario tests for Flow Studio: a simulated caller (an LLM playing the test
case's persona) talks to the agent through the real test engine, then the
case's checks are scored. Runs in the background; results land on AgentTestRun.
"""

import asyncio
import json
import logging
import re
import time
import uuid

from app.integrations.credentials import get_llm_key
from app.integrations.sarvam import SarvamAPIError, chat_completion
from app.services.test_engine import TestVariables, actions_from_events, opening_line, run_test_turn
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import AgentTestCase, AgentTestRun, AIAgent
from packages.database.session import session_scope, set_tenant_context

logger = logging.getLogger(__name__)

HANGUP = "[HANGUP]"
CHECK_TYPES = {"must_say", "must_not_say", "must_call_tool", "must_not_call_tool", "must_reach_node", "must_end", "judge"}
_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


async def _caller_line(persona: str, transcript: list[dict], api_key: str | None) -> str:
    """The simulated caller's next utterance (agent lines are 'user' for this model)."""
    history = [{"role": "user" if t["role"] == "assistant" else "assistant", "content": t["content"]} for t in transcript]
    completion = await chat_completion(
        messages=[
            {
                "role": "system",
                "content": "You are role-playing a CALLER on a phone call to a business. Stay in character.\n"
                f"Your persona and goal: {persona}\n\n"
                "Rules: say one short, natural spoken line per reply (no narration, no stage directions). "
                f"When your goal is done, or the agent says goodbye, reply with exactly {HANGUP}.",
            },
            *(history or [{"role": "user", "content": "(The call connects. Say your first line.)"}]),
        ],
        api_key=api_key,
        temperature=0.7,
        max_tokens=4096,
        timeout=60.0,
    )
    text = _THINK.sub("", completion["choices"][0]["message"].get("content") or "").strip()
    return text or HANGUP


async def _judge(criterion: str, transcript: list[dict], api_key: str | None) -> tuple[bool, str]:
    lines = "\n".join(f'{"Caller" if t["role"] == "user" else "Agent"}: {t["content"]}' for t in transcript)
    try:
        completion = await chat_completion(
            messages=[
                {"role": "system", "content": 'You grade a call transcript against one criterion. Reply ONLY with JSON: {"passed": true|false, "reason": "<under 25 words>"}.'},
                {"role": "user", "content": f"Criterion: {criterion}\n\nTranscript:\n{lines}"},
            ],
            api_key=api_key,
            temperature=0,
            max_tokens=4096,
            timeout=60.0,
        )
        text = _THINK.sub("", completion["choices"][0]["message"].get("content") or "")
        m = _JSON_OBJ.search(text)
        data = json.loads(m.group(0)) if m else {}
        return bool(data.get("passed")), str(data.get("reason") or "")[:300]
    except (SarvamAPIError, ValueError, KeyError) as exc:
        return False, f"Judge unavailable: {exc}"[:300]


def _tools_called(transcript: list[dict]) -> set[str]:
    return {e["name"] for t in transcript for e in (t.get("events") or []) if e.get("type") == "tool"}


async def evaluate(checks: list[dict], transcript: list[dict], nodes: list[dict], api_key: str | None) -> list[dict]:
    agent_text = " \n".join(t["content"] for t in transcript if t["role"] == "assistant").lower()
    tools = _tools_called(transcript)
    reached = {t.get("node_id") for t in transcript if t.get("node_id")}
    titles = {n.get("id"): n.get("title") or n.get("id") for n in nodes}
    ended = any(t.get("ended") for t in transcript)
    out = []
    for c in checks:
        kind, value = c.get("type"), str(c.get("value") or "").strip()
        ok, detail = False, ""
        if kind == "must_say":
            ok = value.lower() in agent_text
            detail = "Said it" if ok else f'Never said "{value}"'
        elif kind == "must_not_say":
            ok = value.lower() not in agent_text
            detail = "Never said it" if ok else f'Said "{value}"'
        elif kind == "must_call_tool":
            ok = value in tools
            detail = f"Called {value}" if ok else f"{value} was not called (called: {', '.join(sorted(tools)) or 'none'})"
        elif kind == "must_not_call_tool":
            ok = value not in tools
            detail = "Not called" if ok else f"{value} was called"
        elif kind == "must_reach_node":
            ok = value in reached
            detail = f'Reached "{titles.get(value, value)}"' if ok else f'Never reached "{titles.get(value, value)}"'
        elif kind == "must_end":
            ok = ended
            detail = "The call reached an end step" if ok else "The call never reached an end step"
        elif kind == "judge":
            ok, detail = await _judge(value, transcript, api_key)
        out.append({"check": {"type": kind, "value": value}, "passed": ok, "detail": detail})
    return out


async def run_case(db: AsyncSession, agent: AIAgent, case: AgentTestCase, nodes: list[dict], edges: list[dict]) -> dict:
    """Plays the scenario out and scores it. Returns the AgentTestRun fields."""
    started = time.perf_counter()
    api_key = await get_llm_key(db, agent.tenant_id)
    variables = TestVariables.from_dict(case.variables)
    transcript: list[dict] = []
    total_tokens = 0
    greeting = opening_line(agent)
    if greeting:
        transcript.append({"role": "assistant", "content": greeting, "node_id": None, "events": [], "greeting": True})
    node_id = next((n.get("id") for n in nodes if n.get("type") == "start"), None)
    for turn in range(max(1, min(case.max_turns, 20))):
        caller = case.opening.strip() if turn == 0 and case.opening.strip() else await _caller_line(case.persona, transcript, api_key)
        if caller.strip().upper().startswith(HANGUP):
            break
        transcript.append({"role": "user", "content": caller})
        history = [{"role": t["role"], "content": t["content"]} for t in transcript]
        result = await run_test_turn(
            db=db, agent=agent, nodes=nodes, edges=edges, history=history, variables=variables, voice=True, previous_node_id=node_id,
            prior_actions=[a for t in transcript if t["role"] == "assistant" for a in actions_from_events(t.get("events"))],
        )
        node_id = result.node_id or node_id
        total_tokens += result.usage.get("total_tokens", 0)
        transcript.append(
            {"role": "assistant", "content": result.reply, "node_id": result.node_id, "node_reason": result.node_reason, "events": result.events, "latency_ms": result.latency_ms, "ended": result.ended}
        )
        if result.ended:
            break
    results = await evaluate(case.checks or [], transcript, nodes, api_key)
    passed = all(r["passed"] for r in results) if results else True
    return {
        "status": "passed" if passed else "failed",
        "passed": passed,
        "results": results,
        "transcript": transcript,
        "total_tokens": total_tokens,
        "duration_ms": round((time.perf_counter() - started) * 1000),
    }


BATCH_CONCURRENCY = 3


async def _run_one(tenant_id: uuid.UUID, agent_id: uuid.UUID, run_id: uuid.UUID, nodes: list[dict], edges: list[dict]) -> None:
    async with session_scope() as db:
        await set_tenant_context(db, tenant_id)
        run = await db.get(AgentTestRun, run_id)
        agent = await db.get(AIAgent, agent_id)
        case = await db.get(AgentTestCase, run.case_id) if run and run.case_id else None
        if not run or not agent or not case:
            return
        run.status = "running"
        await db.commit()
        try:
            fields = await run_case(db, agent, case, nodes, edges)
        except SarvamAPIError as exc:
            fields = {"status": "error", "passed": False, "error": str(exc)[:2000]}
        except Exception as exc:  # a bug in one case must not stop the batch
            logger.exception("scenario run %s failed", run_id)
            fields = {"status": "error", "passed": False, "error": f"{type(exc).__name__}: {exc}"[:2000]}
        for k, v in fields.items():
            setattr(run, k, v)
        await db.commit()


async def run_batch(tenant_id: uuid.UUID, agent_id: uuid.UUID, run_ids: list[uuid.UUID], nodes: list[dict], edges: list[dict]) -> None:
    """Background task: run the queued AgentTestRuns, a few at a time. Each run uses
    its own DB session, so they never share a connection."""
    gate = asyncio.Semaphore(BATCH_CONCURRENCY)

    async def guarded(run_id: uuid.UUID) -> None:
        async with gate:
            await _run_one(tenant_id, agent_id, run_id, nodes, edges)

    await asyncio.gather(*(guarded(r) for r in run_ids))
