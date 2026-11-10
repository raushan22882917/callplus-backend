"""Flow Studio test engine + scenario scoring (no network: pure functions only)."""

import asyncio
import uuid

import pytest
from httpx import AsyncClient

from app.services.agent_engine import CONVERSATION_RULES, TOOL_HONESTY_RULES, VOICE_STYLE_RULES
from app.services.scenario_runner import evaluate
from app.services.test_engine import TestVariables, _simulated_tool_result, build_system_prompt

NODES = [
    {"id": "g", "type": "global", "title": "Global", "prompt": "You are Sam."},
    {"id": "s", "type": "start", "title": "Start", "prompt": "Greet."},
    {"id": "b", "type": "agent", "title": "Booking", "prompt": "Book."},
    {"id": "e", "type": "end", "title": "End", "prompt": "Bye."},
]


def test_prompt_matches_live_call_rules():
    voice = build_system_prompt(NODES, [], voice=True)
    chat = build_system_prompt(NODES, [], voice=False)
    # Same order run_turn uses: honesty rules, voice style, then who is calling.
    assert TOOL_HONESTY_RULES + VOICE_STYLE_RULES + CONVERSATION_RULES + "\n\nCaller details:" in voice
    assert TOOL_HONESTY_RULES + "\n\nCaller details:" in chat and VOICE_STYLE_RULES not in chat
    assert "Test setup" not in voice  # no test-only text unless a later start node is chosen


def test_start_node_adds_marked_test_line():
    assert 'already progressed to the step "Booking"' in build_system_prompt(NODES, [], voice=True, start_node_id="b")
    assert "Test setup" not in build_system_prompt(NODES, [], voice=True, start_node_id="s")


def test_simulated_tools_never_touch_real_data_and_use_variables():
    v = TestVariables.from_dict({"caller_name": "Priya", "caller_phone": "+911", "customer": {"plan": "gold"}})
    assert _simulated_tool_result("get_customer", {}, v)["name"] == "Priya"
    # Like a live call, an unknown caller still has a record made from their number.
    anon = _simulated_tool_result("get_customer", {}, TestVariables())
    assert anon["found"] is True and anon["phone"] == "+919876543210"
    assert _simulated_tool_result("get_customer", {"phone": "+91 90000 00000"}, TestVariables())["found"] is False
    assert _simulated_tool_result("create_ticket", {"subject": "Haircut"}, v)["ticket"]["id"].startswith("TEST-")
    assert _simulated_tool_result("transfer_call", {}, v)["transfer_requested"] is True


def test_deterministic_checks():
    transcript = [
        {"role": "user", "content": "book a haircut"},
        {"role": "assistant", "content": "Booked for Saturday!", "node_id": "b", "events": [{"type": "tool", "name": "create_ticket"}]},
        {"role": "assistant", "content": "Bye", "node_id": "e", "ended": True, "events": []},
    ]
    checks = [
        {"type": "must_say", "value": "saturday"},
        {"type": "must_not_say", "value": "monday"},
        {"type": "must_call_tool", "value": "create_ticket"},
        {"type": "must_not_call_tool", "value": "transfer_call"},
        {"type": "must_reach_node", "value": "b"},
        {"type": "must_end", "value": ""},
        {"type": "must_say", "value": "refund"},
    ]
    results = asyncio.run(evaluate(checks, transcript, NODES, None))
    assert [r["passed"] for r in results] == [True, True, True, True, True, True, False]
    assert 'Never said "refund"' in results[-1]["detail"]


def test_interrupted_batches_are_marked_as_errors():
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from app.api.v1.agent_tests import _expire_stale_runs

    now = datetime.now(timezone.utc)
    old, fresh = now - timedelta(minutes=30), now - timedelta(minutes=1)
    stuck = [SimpleNamespace(id=1, batch_id="a", status=s, updated_at=old, passed=None, error=None) for s in ("passed", "running", "queued")]
    live = [SimpleNamespace(id=2, batch_id="b", status="passed", updated_at=fresh, passed=True, error=None), SimpleNamespace(id=3, batch_id="b", status="queued", updated_at=old, passed=None, error=None)]

    assert _expire_stale_runs(stuck + live) is True
    assert [r.status for r in stuck] == ["passed", "error", "error"]
    assert stuck[1].error and stuck[1].passed is False
    assert [r.status for r in live] == ["passed", "queued"]  # batch still making progress


def test_caller_context_never_sends_callers_to_create_an_account():
    from app.services.agent_engine import caller_context, has_real_name

    new = caller_context("+919699234605", "+919699234605")
    assert "ends in 4605" in new and "new caller" in new
    assert "never ask them to create an account" in new and "contact_phone" in new
    known = caller_context("+919699234605", "Roshan")
    assert "Roshan" in known and "new caller" not in known
    assert caller_context(None, "Roshan") == ""
    assert "messaging from" in caller_context("+919699234605", None, voice=False)
    assert not has_real_name("+919699234605", "+919699234605") and has_real_name("Roshan", "+919699234605")
    assert "+919876543210" in build_system_prompt(NODES, [], voice=True)


def test_agent_is_told_what_it_already_did_so_it_does_not_book_twice():
    from app.services.agent_engine import actions_note, describe_action
    from app.services.test_engine import actions_from_events
    from app.tools.handlers import _other_number

    events = [
        {"type": "tool", "name": "get_customer", "arguments": {}, "result": {"found": True}},
        {"type": "tool", "name": "create_ticket", "arguments": {"subject": "Haircut tomorrow 2pm"}, "result": {"ticket_id": "x"}},
        {"type": "tool", "name": "schedule_callback", "arguments": {"requested_time": "5pm"}, "result": {"error": "boom"}},
    ]
    assert actions_from_events(events) == ["Created a ticket: Haircut tomorrow 2pm"]  # lookups and failures skipped
    assert describe_action("transfer_call", {}) is None
    note = actions_note(["Created a ticket: Haircut tomorrow 2pm"])
    assert "do not do these again" in note and "Haircut tomorrow 2pm" in note
    assert actions_note([]) == ""
    # The calling number passed back as "another number" is ignored.
    assert _other_number("919876543210", "+919876543210") is None
    assert _other_number("+91 90000 00000", "+919876543210") == "+91 90000 00000"
    assert _other_number(None, "+919876543210") is None


async def test_a_failed_model_is_not_spoken_as_the_agent(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.integrations.sarvam import SarvamAPIError

    async def down(*, messages, tools=None, api_key=None, **kwargs):
        raise SarvamAPIError("Connect your own Sarvam API key under Integrations.")

    monkeypatch.setattr("app.services.agent_engine.chat_completion", down)
    tag = uuid.uuid4().hex[:8]
    signed = await client.post(
        "/api/v1/auth/signup",
        json={
            "company_name": "Salon",
            "tenant_slug": f"salon-{tag}",
            "full_name": "Owner",
            "email": f"owner-{tag}@example.com",
            "password": "supersecret1",
        },
    )
    assert signed.status_code == 201, signed.text
    headers = {"Authorization": f"Bearer {signed.json()['access_token']}"}
    agent = await client.post("/api/v1/ai-agents", headers=headers, json={"name": "Receptionist"})
    assert agent.status_code == 201, agent.text
    turn = await client.post(
        f"/api/v1/ai-agents/{agent.json()['id']}/test-turn",
        headers=headers,
        json={"messages": [{"role": "user", "content": "hi"}], "channel": "voice"},
    )
    assert turn.status_code == 424, turn.text
    assert "Sarvam" in turn.json()["detail"]
    assert "human" not in turn.json()["detail"]
