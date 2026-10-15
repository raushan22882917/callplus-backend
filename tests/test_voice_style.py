"""The voice prompt: human register, honesty about being an AI, voice gender."""

import pytest
from app.services import agent_engine as ae
from httpx import AsyncClient

from tests.test_streaming_turn import _agent_and_call, _internal


@pytest.mark.parametrize(
    ("voice", "gender"),
    [
        ("priya", "female"),
        ("Ritu", "female"),
        ("simran_en_customer", "female"),
        ("amit", "male"),
        ("kavya_hi_conversational", "female"),
        (None, "male"),  # Sarvam's default voice is Shubh
        ("sunny", None),  # ambiguous or unknown names get no gendered instruction
        ("not-a-voice", None),
    ],
)
def test_voice_gender(voice, gender):
    assert ae.voice_gender(voice) == gender


def test_gender_note_only_when_the_gender_is_known():
    assert "feminine" in ae.gender_note("priya") and "karti hoon" in ae.gender_note("priya")
    assert "masculine" in ae.gender_note("amit")
    assert ae.gender_note("sunny") == ""


def test_human_rules_ban_bot_phrases_and_forbid_claiming_to_be_human():
    rules = ae.HUMAN_VOICE_RULES.lower()
    for phrase in ("i understand your concern", "certainly", "how may i assist you"):
        assert phrase in rules  # listed as things never to say
    assert "never claim to be human" in rules and "ai assistant" in rules
    assert "never guess the caller's gender" in rules


async def _system_prompt_for(client: AsyncClient, monkeypatch, slug: str, voice: str | None) -> str:
    call_id = await _agent_and_call(client, slug=slug)
    if voice:
        from sqlalchemy import select

        from packages.database.models import AIAgent
        from packages.database.session import session_scope

        async with session_scope() as db:
            agent = (await db.execute(select(AIAgent))).scalar_one()
            agent.voice = voice
            await db.commit()
    seen: dict = {}

    async def fake(**kwargs):
        seen["system"] = kwargs["messages"][0]["content"]
        return {"choices": [{"message": {"content": "Sure.", "tool_calls": None}}], "usage": {}}

    monkeypatch.setattr(ae, "chat_completion", fake)
    resp = await client.post(
        f"/api/v1/internal/calls/{call_id}/turn", headers=_internal(), json={"transcript": "hello"}
    )
    assert resp.status_code == 200, resp.text
    return seen["system"]


async def test_live_call_prompt_includes_the_human_rules_and_voice_gender(client: AsyncClient, monkeypatch):
    prompt = await _system_prompt_for(client, monkeypatch, "style-co", "priya")
    assert "Sound like a real person on the phone" in prompt
    assert "Your voice is female" in prompt
    assert "live phone call" in prompt  # the original voice rules are still there
