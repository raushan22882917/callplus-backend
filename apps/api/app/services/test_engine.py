"""The Flow Studio test engine: runs one agent turn exactly the way a live call
does (same compiled flow prompt, same TOOL_HONESTY / VOICE_STYLE rules, same
enabled tools, same tool-calling loop in agent_engine), against the unsaved
canvas, without touching real data:

- Tools are simulated: each returns a realistic result (built from the test's
  caller variables) and nothing is created, sent or transferred. Read-only
  `search_knowledge_base` runs for real so answers match production.
- Nothing is persisted to Conversation/Message.
- A separate "step tracker" model call reads the conversation afterwards and
  reports which flow node the agent is on. The agent itself never sees it, so
  tracking doesn't change the behaviour being tested.

Used by the Test Chat / Test Audio panel (app/api/v1/agent_tests.py), the voice
test sessions the realtime gateway drives, and the scenario test runner.
"""

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field

from app.integrations.credentials import get_sarvam_api_key
from app.integrations.sarvam import SarvamAPIError, chat_completion
from app.services import guardrails
from app.services.agent_engine import (
    CONVERSATION_RULES,
    TOOL_HONESTY_RULES,
    VOICE_STYLE_RULES,
    _enabled_tools_for_agent,
    _run_tool_loop,
    actions_note,
    caller_context,
    clean_for_speech,
    describe_action,
    with_language_hint,
)
from app.services.flow_compiler import compile_flow_to_system_prompt
from app.tools.context import ToolContext
from app.tools.registry import execute_tool
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import AIAgent

logger = logging.getLogger(__name__)

# Read-only tools that are safe to run for real during a test.
REAL_READONLY_TOOLS = {"search_knowledge_base"}
MAX_TEST_HISTORY = 40


@dataclass
class TestVariables:
    """Who is "calling" in a test — feeds the simulated customer-lookup tools."""

    caller_name: str = ""
    caller_phone: str = ""
    customer: dict = field(default_factory=dict)  # extra customer fields (plan, city, notes…)
    tickets: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict | None) -> "TestVariables":
        d = d or {}
        return cls(
            caller_name=str(d.get("caller_name") or "")[:120],
            caller_phone=str(d.get("caller_phone") or "")[:32],
            customer=d.get("customer") if isinstance(d.get("customer"), dict) else {},
            tickets=d.get("tickets") if isinstance(d.get("tickets"), list) else [],
        )


# A test has no real phone line; stand in for the number a live caller would ring from.
DEFAULT_TEST_CALLER_PHONE = "+919876543210"


def _test_caller_phone(v: TestVariables) -> str:
    return v.caller_phone.strip() or DEFAULT_TEST_CALLER_PHONE


def _simulated_tool_result(name: str, args: dict, v: TestVariables) -> dict:
    """What each tool would plausibly return on a live call — without doing it.
    Like production, the caller always has a record (created from the calling number)."""
    ref = uuid.uuid4().hex[:6].upper()
    if name == "get_customer":
        phone = _test_caller_phone(v)
        asked = (args.get("phone") or "").strip()
        if asked and re.sub(r"\D", "", asked)[-10:] != re.sub(r"\D", "", phone)[-10:]:
            return {"found": False, "note": "No saved record for that number. That's fine: carry on helping; the caller's details are saved from this call, so don't ask them to create an account."}
        return {
            "found": True,
            "customer_id": "test-customer",
            "name": v.caller_name or phone,
            "phone": phone,
            **v.customer,
        }
    if name == "list_recent_tickets":
        return {"tickets": v.tickets}
    if name == "create_ticket":
        return {"ticket": {"id": f"TEST-{ref}", "status": "open", "subject": args.get("subject") or args.get("title") or "", "priority": args.get("priority") or "normal"}}
    if name == "add_customer_note":
        return {"note": {"id": f"NOTE-{ref}", "saved": True}}
    if name == "schedule_callback":
        when = args.get("scheduled_for") or args.get("scheduled_at") or args.get("time") or args.get("when")
        return {"callback": {"id": f"CB-{ref}", "scheduled_for": when, "status": "scheduled"}}
    if name == "send_whatsapp_message":
        return {"message": {"id": f"WA-{ref}", "status": "sent"}}
    if name == "create_customer_payment_link":
        return {"payment_link": {"id": f"PL-{ref}", "url": f"https://pay.example.com/test/{ref.lower()}", "amount": args.get("amount"), "status": "created"}}
    if name == "transfer_call":
        return {"transfer_requested": True, "reason": args.get("reason") or "caller asked for a person"}
    return {"ok": True}


def _flow_steps(nodes: list[dict]) -> list[dict]:
    return [
        {"id": n.get("id"), "type": n.get("type"), "title": n.get("title") or n.get("id"), "prompt": (n.get("prompt") or "")[:400]}
        for n in nodes
        if n.get("type") != "global"
    ]


def build_system_prompt(
    nodes: list[dict],
    edges: list[dict],
    *,
    voice: bool,
    start_node_id: str | None = None,
    variables: TestVariables | None = None,
    guardrail_rules: list | None = None,
) -> str:
    """The live-call prompt (compiled flow + the rules run_turn appends, including the
    caller details). A start node other than the flow's own start adds one test-only
    line, marked as such."""
    v = variables or TestVariables()
    prompt = (
        compile_flow_to_system_prompt(nodes, edges)
        + TOOL_HONESTY_RULES
        + ((VOICE_STYLE_RULES + CONVERSATION_RULES) if voice else "")
        + caller_context(_test_caller_phone(v), v.caller_name or None, voice=voice)
        + guardrails.build_guardrail_prompt(guardrail_rules)
    )
    start = next((n for n in nodes if n.get("id") == start_node_id), None)
    if start and start.get("type") != "start":
        prompt += (
            f'\n\n(Test setup: this conversation has already progressed to the step "{start.get("title") or start.get("id")}". '
            "Continue from that step.)"
        )
    return prompt


_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)


async def track_step(nodes: list[dict], history: list[dict], api_key: str | None, previous: str | None) -> dict:
    """Asks a separate model call which flow step the conversation is on now."""
    steps = _flow_steps(nodes)
    if not steps:
        return {"node_id": None, "reason": ""}
    listing = "\n".join(f'- id="{s["id"]}" ({s["type"]}) {s["title"]}: {s["prompt"][:200]}' for s in steps)
    transcript = "\n".join(f'{"Caller" if m["role"] == "user" else "Agent"}: {m["content"]}' for m in history[-10:])
    try:
        completion = await chat_completion(
            messages=[
                {
                    "role": "system",
                    "content": "You track where a phone conversation is in a call flow. Given the flow's steps and the "
                    "latest transcript, reply with ONLY a JSON object: "
                    '{"node_id": "<id of the step the agent\'s LAST reply belongs to>", "reason": "<under 15 words>"}.',
                },
                {"role": "user", "content": f"Steps:\n{listing}\n\nPrevious step: {previous or 'none'}\n\nTranscript:\n{transcript}"},
            ],
            api_key=api_key,
            temperature=0,
            max_tokens=4096,
            reasoning_effort="low",  # fast model: the step shows up in about a second
            timeout=60.0,
        )
        text = completion["choices"][0]["message"].get("content") or ""
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
        m = _JSON_OBJ.search(text)
        data = json.loads(m.group(0)) if m else {}
        node_id = data.get("node_id")
        if node_id not in {s["id"] for s in steps}:
            node_id = previous
        return {"node_id": node_id, "reason": str(data.get("reason") or "")[:200]}
    except (SarvamAPIError, ValueError, KeyError) as exc:
        logger.info("step tracker failed: %s", exc)
        return {"node_id": previous, "reason": ""}


@dataclass
class TestTurn:
    reply: str
    node_id: str | None
    node_reason: str
    events: list[dict]
    latency_ms: int
    usage: dict
    transfer: bool
    ended: bool


async def run_test_turn(
    *,
    db: AsyncSession,
    agent: AIAgent,
    nodes: list[dict],
    edges: list[dict],
    history: list[dict],
    variables: TestVariables,
    voice: bool,
    start_node_id: str | None = None,
    previous_node_id: str | None = None,
    track: bool = True,
    prior_actions: list[str] | None = None,
) -> TestTurn:
    """One agent reply to `history` (whose last message is the caller's).

    track=False skips the step tracker so the reply comes back at live-call speed;
    the Flow Studio then asks for the node separately (POST .../test-track)."""
    started = time.perf_counter()
    api_key = await get_sarvam_api_key(db, agent.tenant_id)
    events: list[dict] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def on_event(ev: dict) -> None:
        if ev["type"] == "llm":
            for k in usage:
                usage[k] += int((ev.get("usage") or {}).get(k) or 0)
        events.append(ev)

    done_this_turn: list[str] = []

    async def executor(name: str, ctx: ToolContext, args: dict) -> dict:
        if name in REAL_READONLY_TOOLS:
            result = await execute_tool(name, ctx, args)
            events.append({"type": "tool_mode", "name": name, "mode": "real (read-only)"})
            return result
        events.append({"type": "tool_mode", "name": name, "mode": "simulated"})
        # Mirror the live duplicate guard: the same booking twice in one call is one ticket.
        done = describe_action(name, args)
        if name == "create_ticket" and done and done.lower() in {a.lower() for a in [*(prior_actions or []), *done_this_turn]}:
            return {"ticket_id": "TEST-EXISTING", "status": "open", "already_created": True, "note": "This was already booked earlier in this conversation; nothing new was created."}
        if done:
            done_this_turn.append(done)
        return _simulated_tool_result(name, args, variables)

    guardrail_rules = await guardrails.load_rules(db, agent.tenant_id, agent.id)
    messages = [{"role": "system", "content": build_system_prompt(nodes, edges, voice=voice, start_node_id=start_node_id, variables=variables, guardrail_rules=guardrail_rules) + actions_note(prior_actions or [])}, *history[-MAX_TEST_HISTORY:]]
    if voice:
        messages = with_language_hint(messages, agent.language)
    llm_tools = await _enabled_tools_for_agent(db, agent.id)
    tool_ctx = ToolContext(db=db, tenant_id=agent.tenant_id, agent_id=agent.id, call_id=None, conversation_id=None, customer_id=None)
    reply, transfer, _reason = await _run_tool_loop(
        messages=messages, llm_tools=llm_tools, tool_ctx=tool_ctx, sarvam_api_key=api_key, executor=executor, on_event=on_event,
        reasoning_effort="low" if voice else None,
    )
    if voice:
        reply = clean_for_speech(reply) or "Sorry, could you say that again?"
    if guardrail_rules:
        last_caller = next((m["content"] for m in reversed(history) if m.get("role") == "user"), "")
        verdict = guardrails.check_reply(
            guardrail_rules,
            last_caller,
            reply,
            grounding_text="\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "tool"),
            transfer_requested=transfer,
        )
        if not verdict.ok:
            reply, transfer = verdict.reply_text, verdict.transfer_requested
            events.append({"type": "guardrail", "violations": [{"kind": v.kind, "action": v.action, "excerpt": v.excerpt} for v in verdict.violations]})
    agent_ms = round((time.perf_counter() - started) * 1000)

    tracked = (
        await track_step(nodes, [*history, {"role": "assistant", "content": reply}], api_key, previous_node_id or start_node_id)
        if track
        else {"node_id": None, "reason": ""}
    )
    node = next((n for n in nodes if n.get("id") == tracked["node_id"]), None)
    return TestTurn(
        reply=reply,
        node_id=tracked["node_id"],
        node_reason=tracked["reason"],
        events=events,
        latency_ms=agent_ms,
        usage=usage,
        transfer=transfer,
        ended=transfer or (node is not None and node.get("type") == "end"),
    )


def actions_from_events(events: list[dict] | None) -> list[str]:
    """The action tools a test turn ran, in the same words a live call uses."""
    out = []
    for e in events or []:
        if e.get("type") == "tool" and "error" not in (e.get("result") or {}):
            if text := describe_action(e.get("name") or "", e.get("arguments")):
                out.append(text)
    return out


def opening_line(agent: AIAgent) -> str:
    """What a live call says first (the gateway speaks the agent's greeting)."""
    return agent.greeting or ""
