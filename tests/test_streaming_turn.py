"""Streaming LLM turns: sentence splitting, Sarvam SSE parsing, the NDJSON endpoint."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from app.core.config import get_settings
from app.integrations import sarvam
from app.services import agent_engine
from app.services.speech_stream import SentenceStreamer
from httpx import AsyncClient
from sqlalchemy import select

from packages.database.models import Call, CallDirection, CallStatus, Customer, Message, Tenant
from packages.database.session import session_scope


def _split(chunks):
    streamer, out = SentenceStreamer(), []
    for chunk in chunks:
        out += streamer.feed(chunk)
    return out + streamer.flush()


def test_sentences_are_cut_at_terminators_not_inside_numbers():
    assert _split(["Open Mon-Fri. We open at 9.", "5 hours! Call ", "us.", "  Bye"]) == [
        "Open Mon-Fri.",
        "We open at 9.5 hours!",
        "Call us.",
        "Bye",
    ]


def test_hindi_danda_and_markdown_and_urls():
    assert _split(["नमस्ते। आप कैसे हैं? ठीक"]) == ["नमस्ते।", "आप कैसे हैं?", "ठीक"]
    assert _split(["**Sure**, I can help. Visit https://x.com now."]) == ["Sure, I can help.", "Visit now."]


def test_reasoning_blocks_are_never_spoken_even_when_split_across_chunks():
    assert _split(["<thi", "nk>secret. plan</th", "ink>Hello there. How ", "are you?"]) == [
        "Hello there.",
        "How are you?",
    ]
    assert _split(["Fine. <think>never closed. text"]) == ["Fine."]


# ------------------------------------------------------------ SSE parsing


def _sse(*chunks) -> bytes:
    lines = [f"data: {json.dumps(c)}\n\n" for c in chunks] + ["data: [DONE]\n\n"]
    return "".join(lines).encode()


def _delta(**delta):
    return {"choices": [{"delta": {"content": None, "tool_calls": None, **delta}, "finish_reason": None}]}


@pytest.fixture
def mock_sarvam(monkeypatch):
    def install(handler):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(sarvam, "_http", lambda: client)

    return install


async def _collect(**kwargs):
    return [e async for e in sarvam.chat_completion_stream(messages=[{"role": "user", "content": "hi"}], api_key="k", **kwargs)]


async def test_stream_yields_tokens_then_final(mock_sarvam):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=_sse(_delta(content=""), _delta(content="Hel"), _delta(content="lo."), {"usage": {"total_tokens": 5}, "choices": []}))

    mock_sarvam(handler)
    events = await _collect(reasoning_effort="low")
    assert [e["text"] for e in events if e["type"] == "content"] == ["Hel", "lo."]
    assert events[-1] == {"type": "final", "content": "Hello.", "tool_calls": [], "usage": {"total_tokens": 5}}
    assert seen["body"]["stream"] is True and seen["body"]["model"] == sarvam.SARVAM_FAST_CHAT_MODEL


async def test_stream_assembles_fragmented_tool_calls(mock_sarvam):
    mock_sarvam(
        lambda request: httpx.Response(
            200,
            content=_sse(
                _delta(tool_calls=[{"index": 0, "id": "t1", "function": {"name": "get_customer", "arguments": None}}]),
                _delta(tool_calls=[{"index": 0, "id": None, "function": {"name": None, "arguments": '{"phone": "+14'}}]),
                _delta(tool_calls=[{"index": 0, "id": None, "function": {"name": None, "arguments": '155551234"}'}}]),
            ),
        )
    )
    final = (await _collect())[-1]
    assert final["content"] == ""
    assert final["tool_calls"] == [
        {"id": "t1", "type": "function", "function": {"name": "get_customer", "arguments": '{"phone": "+14155551234"}'}}
    ]


async def test_stream_falls_back_to_a_normal_request_when_it_cannot_start(mock_sarvam, monkeypatch):
    mock_sarvam(lambda request: httpx.Response(500, text="boom"))

    async def fake_completion(**kwargs):
        return {"choices": [{"message": {"content": "Fallback reply.", "tool_calls": None}}], "usage": {}}

    monkeypatch.setattr(sarvam, "chat_completion", fake_completion)
    events = await _collect()
    assert [e["text"] for e in events if e["type"] == "content"] == ["Fallback reply."]
    assert events[-1]["content"] == "Fallback reply."


async def test_bad_key_is_not_retried_and_midstream_failure_is_raised(mock_sarvam):
    mock_sarvam(lambda request: httpx.Response(401, text="nope"))
    with pytest.raises(sarvam.SarvamAPIError, match="401"):
        await _collect()

    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield _sse(_delta(content="Hi there."))[: -len(b"data: [DONE]\n\n")]
            raise httpx.ReadError("connection dropped")

    mock_sarvam(lambda request: httpx.Response(200, stream=Broken()))
    got = []
    with pytest.raises(sarvam.SarvamAPIError, match="interrupted"):
        async for event in sarvam.chat_completion_stream(messages=[], api_key="k"):
            got.append(event)
    assert got and got[0]["text"] == "Hi there."


# ------------------------------------------------------------ the endpoint


def _internal():
    return {"X-Internal-Token": get_settings().internal_service_token}


async def _agent_and_call(client: AsyncClient, slug="stream-co", started_ago=5):
    signup = await client.post(
        "/api/v1/auth/signup",
        json={"company_name": slug.title(), "tenant_slug": slug, "full_name": "O", "email": f"o@{slug}.com", "password": "supersecret1"},
    )
    headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}
    agent = (await client.post("/api/v1/ai-agents", headers=headers, json={"name": "Voice", "max_call_duration_seconds": 30})).json()
    async with session_scope() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one()
        customer = Customer(tenant_id=tenant.id, name="Jane", phone="+15550001111")
        db.add(customer)
        await db.flush()
        call = Call(
            tenant_id=tenant.id,
            customer_id=customer.id,
            ai_agent_id=uuid.UUID(agent["id"]),
            direction=CallDirection.INBOUND,
            status=CallStatus.IN_PROGRESS,
            started_at=datetime.now(UTC) - timedelta(seconds=started_ago),
        )
        db.add(call)
        await db.commit()
        return call.id


def _fake_stream(rounds):
    """rounds: one list of events per LLM call, ending in a "final" event."""
    calls = iter(rounds)

    async def fake(**kwargs):
        for event in next(calls):
            yield event

    return fake


def _content(*parts):
    return [{"type": "content", "text": p} for p in parts] + [
        {"type": "final", "content": "".join(parts), "tool_calls": [], "usage": {}}
    ]


async def _post_stream(client, call_id, text="hello"):
    resp = await client.post(f"/api/v1/internal/calls/{call_id}/turn/stream", headers=_internal(), json={"transcript": text})
    assert resp.status_code == 200, resp.text
    return [json.loads(line) for line in resp.text.splitlines() if line]


async def test_endpoint_streams_sentences_then_done_and_saves_what_was_said(client: AsyncClient, monkeypatch):
    call_id = await _agent_and_call(client)
    monkeypatch.setattr(agent_engine, "chat_completion_stream", _fake_stream([_content("Sure, ", "I can help. ", "What time?")]))
    events = await _post_stream(client, call_id, "I need a booking")
    assert [e["text"] for e in events if e["type"] == "sentence"] == ["Sure, I can help.", "What time?"]
    done = events[-1]
    assert done["type"] == "done" and done["reply_text"] == "Sure, I can help. What time?" and done["action"] == "none"
    async with session_scope() as db:
        texts = [m.text for m in (await db.execute(select(Message).order_by(Message.created_at))).scalars()]
        assert texts == ["I need a booking", "Sure, I can help. What time?"]
        assert str(done["reply_message_id"]) in [str(m.id) for m in (await db.execute(select(Message))).scalars()]


async def test_endpoint_runs_a_tool_round_then_streams_the_answer(client: AsyncClient, monkeypatch):
    call_id = await _agent_and_call(client, slug="tool-co")
    tool_round = [
        {
            "type": "final",
            "content": "",
            "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "get_customer", "arguments": json.dumps({"phone": "+15550001111"})}}],
            "usage": {},
        }
    ]
    seen_messages = []
    rounds = iter([tool_round, _content("Found you, Jane.")])

    async def fake(**kwargs):
        seen_messages.append(list(kwargs["messages"]))
        for event in next(rounds):
            yield event

    monkeypatch.setattr(agent_engine, "chat_completion_stream", fake)
    events = await _post_stream(client, call_id)
    assert [e["text"] for e in events if e["type"] == "sentence"] == ["Found you, Jane."]
    # the second LLM call saw the assistant tool call and the tool's result
    roles = [m["role"] for m in seen_messages[1]]
    assert roles[-2:] == ["assistant", "tool"]


async def test_endpoint_survives_an_llm_failure_with_a_spoken_fallback(client: AsyncClient, monkeypatch):
    call_id = await _agent_and_call(client, slug="fail-co")

    async def boom(**kwargs):
        raise sarvam.SarvamAPIError("down")
        yield  # pragma: no cover

    monkeypatch.setattr(agent_engine, "chat_completion_stream", boom)
    events = await _post_stream(client, call_id)
    assert events[0]["type"] == "sentence" and "trouble" in events[0]["text"]
    assert events[-1]["type"] == "done"


async def test_endpoint_failure_after_words_keeps_what_was_spoken(client: AsyncClient, monkeypatch):
    call_id = await _agent_and_call(client, slug="half-co")

    async def half(**kwargs):
        yield {"type": "content", "text": "We open at nine. We cl"}
        raise sarvam.SarvamAPIError("dropped")

    monkeypatch.setattr(agent_engine, "chat_completion_stream", half)
    events = await _post_stream(client, call_id)
    assert [e["text"] for e in events if e["type"] == "sentence"] == ["We open at nine."]
    assert events[-1]["reply_text"] == "We open at nine."


async def test_endpoint_early_returns_only_done(client: AsyncClient):
    call_id = await _agent_and_call(client, slug="late-co", started_ago=120)
    events = await _post_stream(client, call_id)
    assert len(events) == 1 and events[0]["type"] == "done" and events[0]["action"] == "hangup"


async def test_endpoint_requires_the_internal_token_and_reports_missing_calls(client: AsyncClient):
    assert (await client.post(f"/api/v1/internal/calls/{uuid.uuid4()}/turn/stream", json={"transcript": "x"})).status_code == 401
    events = await _post_stream(client, uuid.uuid4())
    assert events[0]["type"] == "error"
