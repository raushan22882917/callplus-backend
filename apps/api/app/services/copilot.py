"""Agent copilot: live suggestions for a human agent handling a call, and
on-demand call summaries. One LLM call per request (JSON-only reply, parsed
defensively); knowledge-base hits come from app/services/knowledge.search and
compliance warnings combine the model's view with the same deterministic
rules the QA scorer uses (forbidden claims, price/refund statements)."""

import json
import re
import uuid

from app.integrations.credentials import get_sarvam_api_key
from app.integrations.sarvam import SarvamAPIError, chat_completion
from app.services import knowledge
from app.services.qa import PRICE_CLAIM, REFUND_CLAIM, Turn, get_default_scorecard, load_turns
from sqlalchemy.ext.asyncio import AsyncSession

SUGGEST_PROMPT = (
    "You are a real-time copilot for a human call-centre agent. You get the conversation so far "
    "and the caller's latest message. Reply with ONLY a JSON object with keys: "
    '"suggested_reply": the next thing the agent should say, in the caller\'s language, 1-3 short sentences; '
    '"intent": the caller\'s current intent in under 8 English words; '
    '"next_action": one short English instruction for the agent (e.g. "Verify the order number"); '
    '"compliance_warning": an English warning if the agent must avoid or verify something here '
    "(promises, refunds, prices, legal/medical advice), else null; "
    '"upsell_hint": one short English upsell/cross-sell suggestion only if the caller shows buying '
    "interest and it would not annoy them, else null; "
    '"translation_en": the caller\'s latest message translated to English (same text if already English). '
    "Use the knowledge-base excerpts when provided; never invent facts, prices or policies not in them."
)
SUMMARIZE_PROMPT = (
    "Summarise this phone conversation for a call-centre agent. Reply with ONLY a JSON object with keys: "
    '"summary": 2-4 English sentences; "key_points": list of up to 6 short English bullet strings; '
    '"action_items": list of short English follow-ups (empty if none); '
    '"sentiment": positive, neutral or negative (the caller\'s mood now). '
    "The conversation may be in any Indian language; always answer in English."
)

_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class CopilotError(Exception):
    pass


def _loads(text: str) -> dict | None:
    text = re.sub(r"<think>.*", "", _THINK.sub("", text or ""), flags=re.DOTALL | re.IGNORECASE)
    match = _JSON_OBJ.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _s(value, limit: int) -> str | None:
    text = str(value).strip() if value not in (None, "") else ""
    return text[:limit] if text and text.lower() not in ("null", "none") else None


def parse_suggestion(text: str) -> dict | None:
    data = _loads(text)
    if data is None or not _s(data.get("suggested_reply"), 1000):
        return None
    return {
        "suggested_reply": _s(data.get("suggested_reply"), 1000),
        "intent": _s(data.get("intent"), 160),
        "next_action": _s(data.get("next_action"), 300),
        "compliance_warning": _s(data.get("compliance_warning"), 400),
        "upsell_hint": _s(data.get("upsell_hint"), 300),
        "translation_en": _s(data.get("translation_en"), 2000),
    }


def parse_summary(text: str) -> dict | None:
    data = _loads(text)
    if data is None or not _s(data.get("summary"), 2000):
        return None
    sentiment = str(data.get("sentiment") or "").lower()

    def _list(value, n):
        return [t for t in (_s(i, 240) for i in value) if t][:n] if isinstance(value, list) else []

    return {
        "summary": _s(data.get("summary"), 2000),
        "key_points": _list(data.get("key_points"), 6),
        "action_items": _list(data.get("action_items"), 10),
        "sentiment": sentiment if sentiment in {"positive", "neutral", "negative"} else "neutral",
    }


def reply_compliance_warnings(reply: str, forbidden_claims: list[str]) -> list[str]:
    """Deterministic check of the suggested reply itself."""
    warnings = []
    for phrase in forbidden_claims:
        if phrase.strip() and phrase.lower() in reply.lower():
            warnings.append(f'The suggested reply contains a forbidden claim: "{phrase}".')
    if REFUND_CLAIM.search(reply):
        warnings.append("The suggested reply mentions a refund/guarantee; confirm policy before saying it.")
    elif PRICE_CLAIM.search(reply):
        warnings.append("The suggested reply quotes a price; confirm it before saying it.")
    return warnings


def format_turns(turns: list[Turn]) -> str:
    return "\n".join(f"{'Agent' if t.speaker == 'agent' else 'Caller'}: {t.text}" for t in turns)


async def _ask(db: AsyncSession, tenant_id: uuid.UUID, system: str, user: str, timeout: float) -> str:
    try:
        completion = await chat_completion(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            api_key=await get_sarvam_api_key(db, tenant_id),
            temperature=0.2,
            max_tokens=2048,
            timeout=timeout,
            reasoning_effort="low",
        )
    except SarvamAPIError as exc:
        raise CopilotError(str(exc)) from exc
    return completion["choices"][0]["message"].get("content") or ""


async def suggest(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    caller_text: str,
    call_id: uuid.UUID | None = None,
    conversation: list[Turn] | None = None,
) -> dict:
    turns = list(conversation or [])
    if call_id is not None and not turns:
        turns = await load_turns(db, tenant_id, call_id)
    kb_hits = await knowledge.search(db, tenant_id=tenant_id, query=caller_text, limit=3)

    parts = []
    if turns:
        parts.append("Conversation so far:\n" + format_turns(turns[-30:]))
    if kb_hits:
        parts.append(
            "Knowledge-base excerpts:\n"
            + "\n".join(f"[{h['title']}] {h['content'][:600]}" for h in kb_hits)
        )
    parts.append(f"Caller's latest message:\n{caller_text}")
    raw = await _ask(db, tenant_id, SUGGEST_PROMPT, "\n\n".join(parts), 45.0)
    parsed = parse_suggestion(raw)
    if parsed is None:
        raise CopilotError("The model did not return a usable suggestion; try again")

    scorecard = await get_default_scorecard(db, tenant_id)
    warnings = reply_compliance_warnings(
        parsed["suggested_reply"], list(scorecard.forbidden_claims) if scorecard else []
    )
    if parsed["compliance_warning"]:
        warnings.insert(0, parsed["compliance_warning"])
    return {
        **parsed,
        "compliance_warning": " ".join(warnings) if warnings else None,
        "knowledge_hits": kb_hits,
    }


async def summarize(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    call_id: uuid.UUID | None = None,
    conversation: list[Turn] | None = None,
) -> dict:
    turns = list(conversation or [])
    if call_id is not None and not turns:
        turns = await load_turns(db, tenant_id, call_id)
    if not turns:
        raise CopilotError("There is no conversation to summarise yet")
    raw = await _ask(db, tenant_id, SUMMARIZE_PROMPT, format_turns(turns)[-12000:], 60.0)
    parsed = parse_summary(raw)
    if parsed is None:
        raise CopilotError("The model did not return a usable summary; try again")
    return parsed
