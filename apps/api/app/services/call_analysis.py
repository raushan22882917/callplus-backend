"""Post-call analysis: one LLM pass over the transcript produces the summary,
sentiment, intent, outcome and action items stored on the Call row. Runs in a
Celery task after the call is finalized (never in the call's request path), and
can be re-run on demand from POST /calls/{id}/analyze.
"""

import json
import logging
import re
import uuid
from datetime import UTC, datetime

from app.integrations.credentials import get_sarvam_api_key
from app.integrations.sarvam import SarvamAPIError, chat_completion
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Call,
    CampaignContact,
    Conversation,
    Message,
    MessageSender,
)

logger = logging.getLogger(__name__)

SENTIMENTS = {"positive", "neutral", "negative"}
OUTCOMES = {
    "resolved",
    "booking_requested",
    "callback_requested",
    "information_given",
    "escalated",
    "sale_interest",
    "complaint",
    "no_conversation",
    "other",
}

ANALYSIS_PROMPT = (
    "You analyse a phone-call transcript between a business's AI agent and a caller. "
    "Reply with ONLY a JSON object, no other text, with these keys: "
    '"summary": 1-3 sentences in English describing what the caller wanted and what happened; '
    '"sentiment": one of positive, neutral, negative (the caller\'s mood at the end); '
    '"intent": the caller\'s main goal in under 8 English words; '
    f'"outcome": one of {", ".join(sorted(OUTCOMES))}; '
    '"action_items": a list of short English follow-ups the business must do (empty list if none). '
    "The transcript may be in any Indian language; always answer in English."
)

_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def parse_analysis(text: str) -> dict | None:
    """Pulls the JSON object out of a model reply and clamps every field to its allowed values."""
    text = re.sub(r"<think>.*", "", _THINK.sub("", text), flags=re.DOTALL | re.IGNORECASE)
    match = _JSON_OBJ.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    sentiment = str(data.get("sentiment") or "").lower()
    outcome = str(data.get("outcome") or "").lower()
    items = data.get("action_items")
    return {
        "summary": str(data.get("summary") or "").strip()[:2000] or None,
        "sentiment": sentiment if sentiment in SENTIMENTS else "neutral",
        "intent": str(data.get("intent") or "").strip()[:160] or None,
        "outcome": outcome if outcome in OUTCOMES else "other",
        "action_items": [str(i).strip()[:240] for i in items if str(i).strip()][:10]
        if isinstance(items, list)
        else [],
    }


async def _rescore_customer_leads(db: AsyncSession, call: Call) -> None:
    """New sentiment/outcome changes how promising the caller's CRM leads are.
    Best effort: a scoring problem must never undo the stored analysis."""
    if call.customer_id is None:
        return
    try:
        from app.services.crm import rescore_lead

        from packages.database.models import CrmLead

        leads = (
            (
                await db.execute(
                    select(CrmLead).where(
                        CrmLead.tenant_id == call.tenant_id, CrmLead.customer_id == call.customer_id
                    )
                )
            )
            .scalars()
            .all()
        )
        for lead in leads:
            await rescore_lead(db, lead)
        await db.commit()
    except Exception:
        await db.rollback()
        logger.warning("could not rescore leads for call %s", call.id, exc_info=True)


def parse_answers(text: str, questions: list[str]) -> dict[str, str]:
    """{question: answer} for the questions the person actually answered; others are left out."""
    text = re.sub(r"<think>.*", "", _THINK.sub("", text), flags=re.DOTALL | re.IGNORECASE)
    match = _JSON_OBJ.search(text)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    answers: dict[str, str] = {}
    for question in questions:
        value = data.get(question)
        if isinstance(value, (str, int, float)) and str(value).strip():
            answer = str(value).strip()[:500]
            if answer.lower() not in {"null", "none", "unknown", "not answered", "n/a"}:
                answers[question] = answer
    return answers


async def _collect_answers(db: AsyncSession, call: Call, transcript: str) -> None:
    """For a campaign call: pull the answers to its questions out of the transcript and file them
    on the call and on the campaign contact, so the owner sees a table of what each person said."""
    questions = [str(q) for q in ((call.mission or {}).get("questions") or [])]
    if not questions:
        return
    prompt = (
        "From the phone-call transcript, extract the person's answer to each question. Reply with ONLY "
        "a JSON object whose keys are exactly the questions below and whose values are short English "
        "answers. Use null for a question that was not answered. Never invent an answer.\n"
        + "\n".join(f"- {q}" for q in questions)
    )
    try:
        completion = await chat_completion(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": transcript[-12000:]},
            ],
            api_key=await get_sarvam_api_key(db, call.tenant_id),
            temperature=0,
            max_tokens=2048,
            timeout=60.0,
            reasoning_effort="low",
        )
    except SarvamAPIError as exc:
        logger.warning("answer extraction failed for %s: %s", call.id, exc)
        return
    answers = parse_answers(completion["choices"][0]["message"].get("content") or "", questions)
    call.answers = answers
    campaign_id = (call.mission or {}).get("campaign_id")
    if campaign_id and call.customer_id:
        contact = (
            await db.execute(
                select(CampaignContact).where(
                    CampaignContact.campaign_id == uuid.UUID(campaign_id),
                    CampaignContact.customer_id == call.customer_id,
                )
            )
        ).scalar_one_or_none()
        if contact is not None:
            contact.result = {
                **(contact.result or {}),
                "answers": answers,
                "call_id": str(call.id),
                "summary": call.summary,
            }
    await db.commit()


async def analyze_call(db: AsyncSession, call_id: uuid.UUID) -> bool:
    """Returns True if the analysis was stored. Never raises for model problems."""
    call = await db.get(Call, call_id)
    if call is None:
        return False
    conversation = (
        await db.execute(select(Conversation).where(Conversation.call_id == call_id))
    ).scalar_one_or_none()
    messages: list[Message] = []
    if conversation is not None:
        messages = list(
            (
                await db.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation.id)
                    .order_by(Message.created_at.asc())
                )
            )
            .scalars()
            .all()
        )

    caller_turns = [m for m in messages if m.sender == MessageSender.CUSTOMER]
    if not caller_turns:
        call.summary = "The caller did not speak."
        call.sentiment = "neutral"
        call.outcome = "no_conversation"
        call.action_items = []
        call.analyzed_at = datetime.now(UTC)
        await db.commit()
        return True

    transcript = "\n".join(
        f"{'Agent' if m.sender == MessageSender.AI_AGENT else 'Caller'}: {m.text}" for m in messages
    )
    try:
        completion = await chat_completion(
            messages=[
                {"role": "system", "content": ANALYSIS_PROMPT},
                {"role": "user", "content": transcript[-12000:]},
            ],
            api_key=await get_sarvam_api_key(db, call.tenant_id),
            temperature=0,
            max_tokens=4096,
            timeout=60.0,
            reasoning_effort="low",
        )
    except SarvamAPIError as exc:
        logger.warning("call analysis failed for %s: %s", call_id, exc)
        return False

    result = parse_analysis(completion["choices"][0]["message"].get("content") or "")
    if result is None:
        logger.warning("call analysis for %s returned no JSON", call_id)
        return False
    call.summary = result["summary"]
    call.sentiment = result["sentiment"]
    call.intent = result["intent"]
    call.outcome = result["outcome"]
    call.action_items = result["action_items"]
    call.analyzed_at = datetime.now(UTC)
    await db.commit()
    await _collect_answers(db, call, transcript)
    await _rescore_customer_leads(db, call)
    return True
