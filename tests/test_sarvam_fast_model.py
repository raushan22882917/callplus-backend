"""Live calls use Sarvam's fast conversation model and fall back to the reasoning model."""

import asyncio

import httpx
import pytest
from app.integrations import sarvam


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.models = []

    async def post(self, url, *, headers, json, timeout):
        self.models.append(json["model"])
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        status, body = reply
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))


def answer(text):
    return (200, {"choices": [{"message": {"content": text}}]})


def run(client, monkeypatch, **kwargs):
    monkeypatch.setattr(sarvam, "_http", lambda: client)
    return asyncio.run(sarvam.chat_completion(messages=[{"role": "user", "content": "hi"}], api_key="k", **kwargs))


def test_low_effort_uses_fast_model(monkeypatch):
    client = FakeClient([answer("Hello!")])
    out = run(client, monkeypatch, reasoning_effort="low")
    assert client.models == [sarvam.SARVAM_FAST_CHAT_MODEL]
    assert out["choices"][0]["message"]["content"] == "Hello!"


@pytest.mark.parametrize("failure", [(500, {"error": "down"}), (200, {"choices": [{"message": {"content": ""}}]}), httpx.ReadTimeout("slow")])
def test_fast_model_failure_falls_back_to_reasoning_model(monkeypatch, failure):
    client = FakeClient([failure, answer("Fallback")])
    out = run(client, monkeypatch, reasoning_effort="low")
    assert client.models == [sarvam.SARVAM_FAST_CHAT_MODEL, sarvam.SARVAM_CHAT_MODEL]
    assert out["choices"][0]["message"]["content"] == "Fallback"


def test_bad_key_is_not_retried(monkeypatch):
    client = FakeClient([(401, {"error": "bad key"})])
    with pytest.raises(sarvam.SarvamAPIError):
        run(client, monkeypatch, reasoning_effort="low")
    assert len(client.models) == 1


def test_default_calls_keep_reasoning_model(monkeypatch):
    client = FakeClient([answer("Deep")])
    run(client, monkeypatch)
    assert client.models == [sarvam.SARVAM_CHAT_MODEL]


def test_reply_language_follows_the_callers_script():
    from app.services.agent_engine import reply_language_hint, with_language_hint

    assert "English letters only" in reply_language_hint("Can I book Monday at 9 pm?")
    assert "English letters" in reply_language_hint("haan kal do baje book kar do")  # Hinglish in Latin letters
    assert "in Hindi" in reply_language_hint("कल दो बजे बुक कर दो")
    assert "in Marathi" in reply_language_hint("उद्या दोन वाजता", "mr-IN")
    assert "in Tamil" in reply_language_hint("நாளை இரண்டு மணிக்கு")
    assert reply_language_hint("123 ...") == ""
    msgs = with_language_hint([{"role": "system", "content": "x"}, {"role": "user", "content": "Hello"}])
    assert msgs[-1]["role"] == "system" and "English" in msgs[-1]["content"]
