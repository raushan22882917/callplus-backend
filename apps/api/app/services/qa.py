"""AI QA scoring of a finished call.

One LLM call grades the transcript on four dimensions (compliance,
communication, resolution, sales, each 0-100) and lists problems. Deterministic
rule checks run on the transcript as well (greeting, introduction, scorecard
phrases, forbidden claims, silence gaps, interruptions, closing, price/refund
claims). Rule findings are always reported and subtract fixed penalties from
the dimension they belong to, so a bad call cannot be scored well just because
the model was generous. If the LLM is unavailable the rule-based result is
still stored (llm_used=False) with resolution/sales left null.

Penalty table (points subtracted from the dimension, floor 0):
  compliance:    missing greeting 10, missing introduction 10, forbidden claim 30 each
                 (max 60), price/refund claim 10 each (max 20), missing script phrase 8 each
  communication: silence gap 8 each (max 24), interruption 8 each (max 24), missing closing 15
"""

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.integrations.credentials import get_llm_key
from app.integrations.sarvam import SarvamAPIError, chat_completion
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Call, Conversation, Message, MessageSender
from packages.database.models.insights import CallQAScore, QAScorecard

logger = logging.getLogger(__name__)

DEFAULT_GREETINGS = (
    "hello", "hi", "hey", "namaste", "namaskar", "good morning", "good afternoon",
    "good evening", "welcome", "thank you for calling", "vanakkam", "sat sri akal",
    "नमस्ते", "नमस्कार", "हैलो", "स्वागत",
)  # fmt: skip
DEFAULT_CLOSINGS = (
    "thank you", "thanks", "goodbye", "bye", "have a great day", "have a nice day",
    "take care", "dhanyavad", "shukriya", "धन्यवाद", "शुक्रिया", "अलविदा",
)  # fmt: skip
INTRO_PATTERNS = re.compile(
    r"\b(this is|my name is|i am|i'm|speaking|calling from|assistant|virtual agent)\b|मैं|मेरा नाम",
    re.IGNORECASE,
)
PRICE_CLAIM = re.compile(
    r"(₹|\brs\.?\s?\d|\brupees?\b|\binr\b|\$\s?\d|\b\d[\d,]*\s?(rupees?|rs|dollars?)\b|\bdiscount\b|\bfree of charge\b)",
    re.IGNORECASE,
)
REFUND_CLAIM = re.compile(
    r"\b(refund(ed|s)?|money back|full refund|guarantee[ds]?|cashback)\b", re.IGNORECASE
)
UNFINISHED_END = re.compile(r"(-|—|…|\.\.\.)\s*$")
INTERRUPT_WINDOW_SECONDS = 2.0
DEFAULT_MAX_SILENCE_SECONDS = 20
EARLY_TURNS = 3
CLOSING_TURNS = 2

DIMENSIONS = ("compliance", "communication", "resolution", "sales")
SEVERITIES = {"critical", "major", "minor"}

QA_PROMPT = (
    "You are a call-centre quality auditor reviewing a phone call between a business's AI agent "
    "and a caller. Reply with ONLY a JSON object, no other text, with keys: "
    '"compliance": 0-100 (followed policy, no false or unauthorised promises, no forbidden claims); '
    '"communication": 0-100 (polite, clear, empathetic, concise); '
    '"resolution": 0-100 (did the agent resolve or correctly route the caller\'s need); '
    '"sales": 0-100 for how well the agent handled any buying/upsell opportunity, or null if the '
    "call had no sales angle; "
    '"summary": one English sentence on the overall quality; '
    '"problems": a list (maybe empty) of objects {"severity": "critical"|"major"|"minor", '
    '"message": short English description, "evidence": the exact short quote from the transcript}. '
    "The transcript may be in any Indian language; always answer in English."
)

_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


@dataclass
class Turn:
    speaker: str  # "agent" | "caller"
    text: str
    at: datetime | None = None


def _clamp(value, *, nullable: bool = False) -> int | None:
    if value is None or isinstance(value, bool):
        return None if nullable else 0
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return None if nullable else 0
    return max(0, min(100, number))


def parse_qa(text: str) -> dict | None:
    """Pulls the JSON object out of a model reply and clamps every field."""
    text = re.sub(r"<think>.*", "", _THINK.sub("", text or ""), flags=re.DOTALL | re.IGNORECASE)
    match = _JSON_OBJ.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    scores = {
        "compliance": _clamp(data.get("compliance"), nullable=True),
        "communication": _clamp(data.get("communication"), nullable=True),
        "resolution": _clamp(data.get("resolution"), nullable=True),
        "sales": _clamp(data.get("sales"), nullable=True),
    }
    if all(v is None for v in scores.values()):
        return None
    problems = []
    raw = data.get("problems")
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            item = {"message": item}
        if not isinstance(item, dict) or not str(item.get("message") or "").strip():
            continue
        severity = str(item.get("severity") or "minor").lower()
        problems.append(
            {
                "code": "llm_finding",
                "severity": severity if severity in SEVERITIES else "minor",
                "message": str(item["message"]).strip()[:300],
                "evidence": (str(item.get("evidence")).strip()[:300] if item.get("evidence") else None),
                "source": "llm",
            }
        )
    return {
        **scores,
        "summary": str(data.get("summary") or "").strip()[:500] or None,
        "problems": problems[:15],
    }


def _contains_any(text: str, phrases) -> bool:
    lowered = text.lower()
    return any(p.lower() in lowered for p in phrases if p and p.strip())


def _problem(code: str, severity: str, message: str, evidence: str | None = None) -> dict:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "evidence": evidence[:300] if evidence else None,
        "source": "rule",
    }


def deterministic_checks(turns: list[Turn], scorecard: QAScorecard | None = None) -> dict:
    """Returns {"problems": [...], "penalties": {"compliance": int, "communication": int}}."""
    problems: list[dict] = []
    compliance_pen = 0
    comm_pen = 0
    agent_turns = [t for t in turns if t.speaker == "agent" and t.text.strip()]
    caller_turns = [t for t in turns if t.speaker == "caller" and t.text.strip()]

    greetings = (scorecard.greeting_phrases if scorecard and scorecard.greeting_phrases else None) or DEFAULT_GREETINGS
    closings = (scorecard.closing_phrases if scorecard and scorecard.closing_phrases else None) or DEFAULT_CLOSINGS
    max_silence = scorecard.max_silence_seconds if scorecard else DEFAULT_MAX_SILENCE_SECONDS
    intro_required = scorecard.introduction_required if scorecard else True

    if agent_turns:
        early = " ".join(t.text for t in agent_turns[:EARLY_TURNS])
        if not _contains_any(early, greetings):
            problems.append(_problem("greeting_missing", "major", "The agent did not greet the caller.", agent_turns[0].text))
            compliance_pen += 10
        if intro_required and not INTRO_PATTERNS.search(early):
            problems.append(_problem("introduction_missing", "minor", "The agent did not introduce itself or the business.", agent_turns[0].text))
            compliance_pen += 10

    # Scorecard script phrases must all appear; forbidden claims must not.
    all_agent_text = " ".join(t.text for t in agent_turns)
    if scorecard:
        for phrase in scorecard.script_phrases or []:
            if phrase.strip() and phrase.lower() not in all_agent_text.lower():
                problems.append(_problem("script_phrase_missing", "minor", f'Required phrase not said: "{phrase}".'))
                compliance_pen += 8
        forbidden_pen = 0
        for phrase in scorecard.forbidden_claims or []:
            if not phrase.strip():
                continue
            for t in agent_turns:
                if phrase.lower() in t.text.lower():
                    problems.append(_problem("forbidden_claim", "critical", f'Agent said a forbidden claim: "{phrase}".', t.text))
                    forbidden_pen += 30
                    break
        compliance_pen += min(forbidden_pen, 60)

    claim_pen = 0
    for t in agent_turns:
        if REFUND_CLAIM.search(t.text):
            problems.append(_problem("refund_claim", "major", "The agent made a refund/guarantee statement; verify it matches policy.", t.text))
            claim_pen += 10
        elif PRICE_CLAIM.search(t.text):
            problems.append(_problem("price_claim", "minor", "The agent quoted a price or discount; verify it is correct.", t.text))
            claim_pen += 10
    compliance_pen += min(claim_pen, 20)

    # Silence: a long wait between consecutive turns (whoever is slow).
    silence_pen = 0
    timed = [t for t in turns if t.at is not None and t.text.strip()]
    for prev, cur in zip(timed, timed[1:], strict=False):
        gap = (cur.at - prev.at).total_seconds()
        if gap > max_silence:
            who = "reply" if cur.speaker == "agent" else "response"
            problems.append(_problem("long_silence", "minor", f"{int(gap)}s of silence before the {cur.speaker}'s {who}.", cur.text))
            silence_pen += 8
    comm_pen += min(silence_pen, 24)

    # Interruption: caller was cut off mid-sentence and the agent spoke right after.
    interrupt_pen = 0
    for prev, cur in zip(timed, timed[1:], strict=False):
        if (
            prev.speaker == "caller"
            and cur.speaker == "agent"
            and UNFINISHED_END.search(prev.text.strip())
            and (cur.at - prev.at).total_seconds() <= INTERRUPT_WINDOW_SECONDS
        ):
            problems.append(_problem("agent_interruption", "minor", "The agent spoke over an unfinished caller sentence.", prev.text))
            interrupt_pen += 8
    comm_pen += min(interrupt_pen, 24)

    # Closing only matters if the caller actually conversed.
    if agent_turns and len(caller_turns) >= 2:
        tail = " ".join(t.text for t in agent_turns[-CLOSING_TURNS:])
        if not _contains_any(tail, closings):
            problems.append(_problem("closing_missing", "minor", "The call ended without a closing or thanks.", agent_turns[-1].text))
            comm_pen += 15

    return {"problems": problems, "penalties": {"compliance": compliance_pen, "communication": comm_pen}}


def combine_scores(llm: dict | None, rule: dict) -> dict:
    """Merge the LLM's dimension scores with rule penalties into the stored result."""
    pen = rule["penalties"]
    scores: dict[str, int | None] = {}
    for dim in DIMENSIONS:
        base = llm.get(dim) if llm else None
        if dim in ("compliance", "communication"):
            if base is None:
                base = 100
            scores[dim] = max(0, base - pen.get(dim, 0))
        else:
            scores[dim] = base
    present = [v for v in scores.values() if v is not None]
    overall = round(sum(present) / len(present)) if present else 0
    problems = list(rule["problems"]) + (llm["problems"] if llm else [])
    order = {"critical": 0, "major": 1, "minor": 2}
    problems.sort(key=lambda p: order.get(p["severity"], 3))
    return {
        **scores,
        "overall": overall,
        "problems": problems,
        "summary": llm["summary"] if llm else None,
        "llm_used": llm is not None,
    }


def scorecard_prompt(scorecard: QAScorecard | None) -> str:
    if scorecard is None:
        return QA_PROMPT
    extras = []
    if scorecard.script_phrases:
        extras.append("Required script phrases: " + "; ".join(scorecard.script_phrases))
    if scorecard.forbidden_claims:
        extras.append("Forbidden claims: " + "; ".join(scorecard.forbidden_claims))
    if scorecard.description:
        extras.append("Business QA notes: " + scorecard.description)
    return QA_PROMPT + (" " + " ".join(extras) if extras else "")


def messages_to_turns(messages: list[Message]) -> list[Turn]:
    return [
        Turn(
            speaker="caller" if m.sender == MessageSender.CUSTOMER else "agent",
            text=(m.text or "").strip(),
            at=m.created_at,
        )
        for m in messages
        if (m.text or "").strip()
    ]


async def load_turns(db: AsyncSession, tenant_id: uuid.UUID, call_id: uuid.UUID) -> list[Turn]:
    conversation = (
        await db.execute(
            select(Conversation).where(
                Conversation.tenant_id == tenant_id, Conversation.call_id == call_id
            )
        )
    ).scalar_one_or_none()
    if conversation is None:
        return []
    messages = (
        (
            await db.execute(
                select(Message)
                .where(Message.tenant_id == tenant_id, Message.conversation_id == conversation.id)
                .order_by(Message.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return messages_to_turns(list(messages))


async def get_default_scorecard(db: AsyncSession, tenant_id: uuid.UUID) -> QAScorecard | None:
    return (
        await db.execute(
            select(QAScorecard)
            .where(QAScorecard.tenant_id == tenant_id, QAScorecard.is_default.is_(True))
            .order_by(QAScorecard.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def score_call(
    db: AsyncSession, call_id: uuid.UUID, scorecard_id: uuid.UUID | None = None
) -> CallQAScore | None:
    """Scores (or re-scores) a call. Returns None when there is nothing to score
    (call missing, or the caller never spoke). Never raises for model problems."""
    call = await db.get(Call, call_id)
    if call is None:
        return None
    turns = await load_turns(db, call.tenant_id, call_id)
    if not any(t.speaker == "caller" for t in turns) or not any(t.speaker == "agent" for t in turns):
        return None

    scorecard = None
    if scorecard_id is not None:
        scorecard = await db.get(QAScorecard, scorecard_id)
        if scorecard is not None and scorecard.tenant_id != call.tenant_id:
            scorecard = None
    if scorecard is None:
        scorecard = await get_default_scorecard(db, call.tenant_id)

    rule = deterministic_checks(turns, scorecard)

    llm = None
    transcript = "\n".join(f"{'Agent' if t.speaker == 'agent' else 'Caller'}: {t.text}" for t in turns)
    try:
        completion = await chat_completion(
            messages=[
                {"role": "system", "content": scorecard_prompt(scorecard)},
                {"role": "user", "content": transcript[-12000:]},
            ],
            api_key=await get_llm_key(db, call.tenant_id),
            temperature=0,
            max_tokens=4096,
            timeout=60.0,
            reasoning_effort="low",
        )
        llm = parse_qa(completion["choices"][0]["message"].get("content") or "")
        if llm is None:
            logger.warning("QA scoring for %s returned no usable JSON", call_id)
    except SarvamAPIError as exc:
        logger.warning("QA LLM scoring failed for %s: %s", call_id, exc)

    result = combine_scores(llm, rule)

    row = (
        await db.execute(
            select(CallQAScore).where(
                CallQAScore.tenant_id == call.tenant_id, CallQAScore.call_id == call_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = CallQAScore(tenant_id=call.tenant_id, call_id=call_id)
        db.add(row)
    row.scorecard_id = scorecard.id if scorecard else None
    row.compliance = result["compliance"]
    row.communication = result["communication"]
    row.resolution = result["resolution"]
    row.sales = result["sales"]
    row.overall = result["overall"]
    row.problems = result["problems"]
    row.summary = result["summary"]
    row.llm_used = result["llm_used"]
    row.scored_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(row)
    return row
