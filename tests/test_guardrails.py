from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from app.api.v1 import guardrails as guardrails_api
from app.main import app
from app.services import knowledge as knowledge_service
from app.services.guardrails import build_guardrail_prompt, check_reply, detect_anger
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import KnowledgeDocument
from packages.database.models.guardrails import GuardrailRule  # noqa: F401 — registers tables
from tests.test_call_control import _auth, _signup


def rule(kind, text=None, priority=100, **params):
    return SimpleNamespace(id=None, kind=kind, text=text, params=params, enabled=True, priority=priority)


# ---- prompt ---------------------------------------------------------------


def test_prompt_empty_without_rules_and_skips_disabled() -> None:
    assert build_guardrail_prompt([]) == ""
    off = rule("custom_instruction", "Be rude")
    off.enabled = False
    assert build_guardrail_prompt([off]) == ""


def test_prompt_contains_each_rule_in_priority_order() -> None:
    prompt = build_guardrail_prompt(
        [
            rule("max_discount_percent", percent=15, priority=20),
            rule("never_reveal", "our margins", priority=1),
            rule("working_hours", start="10:00", end="17:00", timezone="Asia/Kolkata"),
            rule("restricted_topic", keywords=["politics", "religion"]),
        ]
    )
    assert prompt.index("our margins") < prompt.index("15 percent")
    assert "10:00 to 17:00" in prompt and "politics, religion" in prompt


# ---- anger across languages ----------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "This is ridiculous, I am fed up",
        "Ye sab bakwas hai yaar",
        "आपकी सर्विस बहुत बेकार है, मैं तंग आ गया",
        "இது மிகவும் மோசம்",
        "ఇది చెత్త సర్వీస్",
        "এটা খুব বাজে",
        "WHERE IS MY ORDER NOW",
        "answer me!!!",
    ],
)
def test_anger_detected(text: str) -> None:
    assert detect_anger(text)


@pytest.mark.parametrize("text", ["Hello, I'd like to book a haircut", "नमस्ते, मुझे अपॉइंटमेंट चाहिए", "what is the price"])
def test_calm_text_not_anger(text: str) -> None:
    assert detect_anger(text) is None


def test_anger_escalates_and_replaces_with_handoff_in_caller_language() -> None:
    v = check_reply([rule("escalate_on_anger")], "बहुत बकवास सर्विस है", "Please calm down.")
    assert v.transfer_requested and v.transfer_reason
    assert "सहकर्मी" in v.reply_text
    assert v.violations[0].action == "escalated"

    en = check_reply([rule("escalate_on_anger")], "this is useless", "Sure, anything else?")
    assert en.transfer_requested and "colleague" in en.reply_text


def test_no_violation_leaves_reply_untouched() -> None:
    v = check_reply([rule("escalate_on_anger"), rule("no_refund_without_auth")], "hi", "Hello, how can I help?")
    assert v.ok and v.reply_text == "Hello, how can I help?" and not v.transfer_requested


# ---- discount cap --------------------------------------------------------


@pytest.mark.parametrize(
    "reply,blocked",
    [
        ("Sure, I can give you 25% discount.", True),
        ("We can do a discount of 30 percent today.", True),
        ("आपको 40 प्रतिशत की छूट मिल जाएगी", True),
        ("I can offer 10% off.", False),
        ("Discount up to 5 percent is possible.", False),
        ("GST is 18% on the bill.", False),
    ],
)
def test_discount_cap(reply: str, blocked: bool) -> None:
    v = check_reply([rule("max_discount_percent", percent=10)], "any discount?", reply)
    assert (not v.ok) is blocked
    if blocked:
        assert "10 percent" in v.reply_text or "10 प्रतिशत" in v.reply_text


# ---- refunds / prices / topics / escalation keywords ---------------------


@pytest.mark.parametrize(
    "reply,blocked",
    [
        ("Don't worry, we will refund your money.", True),
        ("I've processed a refund for you.", True),
        ("Aapka refund kar diya gaya hai.", True),
        ("आपका रिफंड हो जाएगा", True),
        ("I cannot refund this without approval.", False),
        ("Our refund policy is on the website.", False),
    ],
)
def test_refund_promises(reply: str, blocked: bool) -> None:
    v = check_reply([rule("no_refund_without_auth")], "I want my money back", reply)
    assert (not v.ok) is blocked
    assert v.transfer_requested is blocked


def test_invented_price_blocked_but_grounded_price_allowed() -> None:
    rules = [rule("never_invent_price")]
    bad = check_reply(rules, "how much?", "That costs Rs 1,500.", grounding_text="Haircut: Rs 500")
    assert not bad.ok and "1,500" not in bad.reply_text
    ok = check_reply(rules, "how much?", "A haircut is ₹500.", grounding_text="Haircut: Rs 500")
    assert ok.ok
    assert not check_reply(rules, "how much?", "यह ५०० रुपये का है", grounding_text="").ok
    assert check_reply(rules, "I paid 300", "Your 300 rupees payment is noted", grounding_text="").ok


def test_restricted_topic_and_escalate_keyword() -> None:
    topic = check_reply([rule("restricted_topic", "politics, religion")], "Who will win the election? politics", "Well...")
    assert not topic.ok and not topic.transfer_requested
    esc = check_reply([rule("escalate_keyword", keywords=["manager", "शिकायत"])], "मुझे शिकायत दर्ज करनी है", "Sure")
    assert esc.transfer_requested and "शिकायत" in esc.transfer_reason
    # Latin keywords respect word boundaries.
    assert check_reply([rule("escalate_keyword", keywords=["hell"])], "hello there", "hi").ok


def test_low_confidence_escalates() -> None:
    v = check_reply([rule("escalate_low_confidence")], "q", "Hmm, I'm not sure about that.")
    assert v.transfer_requested
    assert check_reply([rule("escalate_low_confidence")], "q", "मुझे नहीं पता", ).transfer_requested


def test_working_hours_cancels_transfer_outside_hours() -> None:
    r = rule("working_hours", start="09:00", end="18:00", timezone="Asia/Kolkata", days=[0, 1, 2, 3, 4])
    night = datetime(2026, 10, 5, 22, 0, tzinfo=ZoneInfo("Asia/Kolkata"))  # Monday 22:00
    day = datetime(2026, 10, 5, 11, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    blocked = check_reply([r], "agent please", "Connecting you.", transfer_requested=True, now=night)
    assert blocked.cancel_transfer and not blocked.transfer_requested and "09:00" in blocked.reply_text
    assert check_reply([r], "agent please", "Connecting you.", transfer_requested=True, now=day).ok


def test_custom_message_and_priority() -> None:
    v = check_reply([rule("restricted_topic", keywords=["gold"], message="Custom safe reply")], "gold rate?", "x")
    assert v.reply_text == "Custom safe reply"


# ---- API + knowledge control ---------------------------------------------

if not any(getattr(r, "path", "").startswith("/api/v1/guardrails") for r in app.routes):
    app.include_router(guardrails_api.router, prefix="/api/v1")


async def test_rule_crud_validation_templates_and_isolation(client: AsyncClient) -> None:
    a = await _signup(client, slug="gr-a", email="a@gr.com")
    b = await _signup(client, slug="gr-b", email="b@gr.com")

    assert (await client.post("/api/v1/guardrails/rules", headers=_auth(a), json={"kind": "max_discount_percent"})).status_code == 422
    assert (await client.post("/api/v1/guardrails/rules", headers=_auth(a), json={"kind": "bogus"})).status_code == 422

    created = await client.post(
        "/api/v1/guardrails/rules", headers=_auth(a),
        json={"kind": "max_discount_percent", "params": {"percent": 12}, "priority": 5},
    )
    assert created.status_code == 201, created.text
    rid = created.json()["id"]

    patched = await client.patch(f"/api/v1/guardrails/rules/{rid}", headers=_auth(a), json={"enabled": False})
    assert patched.json()["enabled"] is False
    bad = await client.patch(f"/api/v1/guardrails/rules/{rid}", headers=_auth(a), json={"params": {"percent": 500}})
    assert bad.status_code == 422

    assert [r["id"] for r in (await client.get("/api/v1/guardrails/rules", headers=_auth(a))).json()] == [rid]
    assert (await client.get("/api/v1/guardrails/rules", headers=_auth(b))).json() == []
    assert (await client.get(f"/api/v1/guardrails/rules/{rid}", headers=_auth(b))).status_code == 404

    templates = (await client.get("/api/v1/guardrails/defaults", headers=_auth(a))).json()
    assert {t["kind"] for t in templates} >= {"escalate_on_anger", "max_discount_percent"}
    assert (await client.get("/api/v1/guardrails/events", headers=_auth(a))).json() == []

    assert (await client.delete(f"/api/v1/guardrails/rules/{rid}", headers=_auth(a))).status_code == 204


async def test_knowledge_ai_control_hides_document_from_search(client: AsyncClient, db_session: AsyncSession) -> None:
    tokens = await _signup(client, slug="gr-kb", email="o@grkb.com")
    doc = await client.post(
        "/api/v1/knowledge/documents", headers=_auth(tokens),
        json={"title": "Pricing", "content": "The platinum membership costs a lot of money."},
    )
    assert doc.status_code == 201, doc.text
    doc_id = doc.json()["id"]
    tenant_id = (await db_session.get(KnowledgeDocument, __import__("uuid").UUID(doc_id))).tenant_id

    assert len(await knowledge_service.search(db_session, tenant_id=tenant_id, query="platinum membership")) == 1

    off = await client.put(f"/api/v1/guardrails/knowledge/{doc_id}", headers=_auth(tokens), json={"enabled": False})
    assert off.status_code == 200 and off.json()["enabled"] is False
    db_session.expire_all()
    assert await knowledge_service.search(db_session, tenant_id=tenant_id, query="platinum membership") == []

    internal = await client.put(
        f"/api/v1/guardrails/knowledge/{doc_id}", headers=_auth(tokens), json={"enabled": True, "visibility": "internal"}
    )
    assert internal.json()["visibility"] == "internal"
    db_session.expire_all()
    hits = await knowledge_service.search(db_session, tenant_id=tenant_id, query="platinum membership")
    assert hits and hits[0]["internal_only"] is True

    listed = (await client.get("/api/v1/guardrails/knowledge", headers=_auth(tokens))).json()
    assert listed[0]["visibility"] == "internal"
    assert (await client.put(f"/api/v1/guardrails/knowledge/{__import__('uuid').uuid4()}", headers=_auth(tokens), json={"enabled": False})).status_code == 404
