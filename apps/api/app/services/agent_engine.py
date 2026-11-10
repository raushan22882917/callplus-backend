"""The "AI Engine": turns a transcript into an agent reply, running the LLM
tool-calling loop against the tools registry.

Two orchestrators share the same tool-calling loop (`_run_tool_loop`):
`run_turn` — customer-facing, tied to a tenant's `AIAgent` — is what the Go
realtime gateway calls per conversational turn (via app/api/v1/internal.py)
and what the WhatsApp webhook calls per inbound message. `run_assistant_turn`
— staff-facing, not tied to any `AIAgent` row — backs the internal assistant
(app/api/v1/assistant.py), reusing the same tool registry with `agent_id=None`,
the same pattern the workflow engine already uses.
"""

import json
import logging
import re
import time
import unicodedata
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.integrations.credentials import get_llm_key
from app.integrations.llm import LlmEndpoint
from app.integrations.sarvam import (
    SARVAM_LANGUAGES,
    SARVAM_TTS_LANGUAGES,
    SarvamAPIError,
    chat_completion,
    chat_completion_stream,
)
from app.services import guardrails
from app.services.customer_context import (
    bookings_note,
    customer_bookings,
    has_bookable_services,
    other_channel_note,
    today_note,
)
from app.services.speech_stream import SentenceStreamer, clean_for_speech
from app.tools.context import ToolContext
from app.tools.registry import TOOL_REGISTRY, execute_tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    AIAgent,
    AIAgentTool,
    Call,
    CallStatus,
    Conversation,
    ConversationChannel,
    Customer,
    Message,
    MessageDirection,
    MessageSender,
    Tenant,
    Ticket,
    TicketStatus,
)

logger = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 4
MAX_HISTORY_MESSAGES = 20

# Tools the internal staff assistant may use. Deliberately excludes the
# customer-channel-only tools (send_whatsapp_message, create_customer_payment_link,
# transfer_call) — those don't mean anything outside a live customer conversation.
ASSISTANT_TOOL_NAMES = (
    "get_customer",
    "list_open_tickets",
    "list_recent_tickets",
    "create_ticket",
    "add_customer_note",
    "schedule_callback",
)

# Tools an agent gets when nobody has ever configured its tools (no AIAgentTool
# rows). Without them the model can only talk — and, as seen on a live call, it
# will happily claim it "scheduled a callback" with nothing recorded anywhere.
# Once any tool row exists the tenant's explicit choices win (disabled stays off).
DEFAULT_AGENT_TOOLS = frozenset(
    {
        "search_knowledge_base",
        "get_customer",
        "create_ticket",
        "add_customer_note",
        "schedule_callback",
        "transfer_call",
    }
)

BOOKING_TOOLS = frozenset(
    {"check_availability", "book_appointment", "reschedule_appointment", "cancel_appointment", "list_my_appointments"}
)
BOOKING_RULES = (
    "\n\nBookings: this business takes real bookings. To book, call check_availability for the service and "
    "day, offer one or two free times, then book_appointment once the customer picks one. Do not open a ticket "
    "for a booking. To move or cancel, use reschedule_appointment or cancel_appointment with the confirmation_id."
)

TOOL_HONESTY_RULES = (
    "\n\nYou can only take real actions (create a ticket, schedule a callback, "
    "look up a customer, transfer a call) by calling the tools you were given. "
    "Never tell the caller you have booked, scheduled, created or sent anything "
    "unless the matching tool call succeeded in this conversation. If a tool "
    "returned an error or you have no tool for it, say plainly that you could not "
    "do it and that a team member will follow up."
)

# Appended to the agent's own prompt on phone calls. The reply is read aloud by
# TTS, so length and formatting directly decide whether the call feels like a
# conversation or a monologue the caller has to sit through.
VOICE_STYLE_RULES = (
    "\n\nYou are speaking on a live phone call. Your reply is read aloud, so: "
    "keep it to one to three short sentences; ask at most one question at a time, "
    "then stop and wait for the caller; never use markdown, bullet points, numbered "
    "lists, emojis, or URLs; spell out numbers and times the way you would say them; "
    "do not repeat what the caller just said or re-introduce yourself; if the caller "
    "interrupts or says something short like 'okay' or 'yes', respond briefly and move on."
)

# What separates a person on the phone from a "voice assistant": register, reactions,
# and the stock phrases customer-service bots keep repeating. Kept short and
# concrete because the fast voice model follows examples far better than adjectives.
HUMAN_VOICE_RULES = (
    "\n\nSound like a real person on the phone, not a script or an assistant. "
    "Talk like a warm, relaxed receptionist in India: everyday words, short sentences, one idea at a time. "
    "Start by reacting to what they just said in a few words when it fits ('Sure, no problem.', 'Oh, okay.', "
    "'Alright.'; only when the caller is speaking Hindi, the Hindi equivalents such as 'जी, बिल्कुल।' or 'अच्छा, ठीक है।'), "
    "but never open two replies in a row the same way, and skip the reaction when the answer is already one short "
    "sentence. "
    "Use contractions in English (I'll, we're, that's). Mix languages only the way the caller does: if their message "
    "mixes Hindi and English, you may too; if it is in one language, stay in it. Write each Hindi word in the script "
    "the script rule below asks for: never put Roman-letter Hindi inside a Devanagari reply or the other way round. "
    "In Hindi use 'आप' naturally; never add a stray 'aap' or 'ji' just to sound Indian. Never guess the caller's "
    "gender: in Hindi use the respectful forms that suit anyone (चाहेंगे, आइए), not feminine or masculine ones. "
    "Never say or write anything like: 'I understand your concern', 'certainly', 'absolutely', 'I apologize for the "
    "inconvenience', 'kindly', 'assist', 'how may I assist you', 'feel free to', 'is there anything else I can "
    "help you with today'. Say it the way a neighbour would: 'Anything else?'. "
    "Do not repeat back what the caller just told you, do not summarise, and do not thank them in every reply. "
    "If you don't know something, say so plainly and say who will find out ('I don't have that right now, I'll "
    "check with the team and let you know'), instead of a long apology. "
    "Say times, prices and dates the way people say them aloud ('six in the evening', 'tomorrow morning', 'five "
    "hundred rupees'), never as digits and symbols. "
    "A very short answer is often the most natural one: 'Ten to eight, every day.' "
    "Be honest about what you are: you are the business's AI assistant. If the caller asks whether you are a robot, "
    "an AI, a machine or a real person, say plainly and warmly that you are an AI assistant, and offer to connect "
    "them to a person if they would prefer. Never claim to be human."
)

FEMALE_VOICES = frozenset(
    "ritu priya neha pooja simran kavya ishita shreya roopa tanya shruti suhani kavitha rupali "
    "anushka manisha vidya arya".split()
)
MALE_VOICES = frozenset(
    "shubh aditya rahul rohan amit dev ratan varun manan sumit kabir aayan ashutosh advait anand tarun "
    "gokul vijay mohit rehan soham abhilash karun hitesh".split()
)


def voice_gender(voice: str | None) -> str | None:
    """"female"/"male" for a known Bulbul voice. Sarvam's default voice (no name
    set) is Shubh. Persona ids such as simran_en_customer use the first part."""
    name = (voice or "shubh").strip().lower().split("_")[0]
    if name in FEMALE_VOICES:
        return "female"
    if name in MALE_VOICES:
        return "male"
    return None


def gender_note(voice: str | None) -> str:
    """Hindi and most Indian languages mark the speaker's gender on verbs
    ("karti hoon" / "karta hoon"). A female voice saying the male form is one of
    the quickest giveaways that a caller is talking to a machine."""
    gender = voice_gender(voice)
    if gender is None:
        return ""
    forms = "feminine (karti hoon, gayi, rahi)" if gender == "female" else "masculine (karta hoon, gaya, raha)"
    return (
        f"\n\nYour voice is {gender}. In Hindi and the other languages that mark gender on verbs, always "
        f"refer to yourself with {forms} forms."
    )


# Behaviour seen on a live salon call that the rules above did not stop: the agent
# switched to English for a Hindi caller, welcomed them a second time, asked for the
# callback number before it knew what was being booked (and offered to "make someone
# call another number"), invented an apology, and kept replying to every "thank you".
CONVERSATION_RULES = (
    "\n\nConversation rules: "
    "Callers may speak any of these languages: "
    f"{', '.join(SARVAM_LANGUAGES.values())}, or a mix such as Hinglish. "
    "Reply in the language the caller used in their latest message. "
    "The phone voice speaks only "
    f"{', '.join(SARVAM_TTS_LANGUAGES.values())} aloud. "
    "For those, use that language's own script "
    "(Devanagari for Hindi and Marathi, Tamil script for Tamil, Latin for English). "
    "For Assamese and Manipuri, use Bengali script. "
    "For every other language, write the reply in Devanagari so the Hindi voice can say it. "
    "For a mix, reply in the same mix. "
    "Do not switch language on your own: if the caller's latest message is in English, "
    "reply only in English, even if earlier messages or the business name are Hindi. "
    "Greet only once, at the start; after that never say welcome, namaste or the business name again. "
    "Collect what you need in this order, one question per turn: what they want, the day and time, "
    "then confirm the contact number. Never ask about the phone number before you know the service and the time. "
    "Never ask anything you already know from the conversation, such as their name or the number they are calling from. "
    "When you confirm the number, say it as a plain yes-or-no question about the number they are calling from "
    "(for example: 'Should we reach you on this same number?'). "
    "Never offer to have someone else call, or to call another number, unless the caller asks for it. "
    "Never read out ticket numbers, reference ids or record ids; just say the request is noted. "
    "Only apologise if something actually went wrong in this conversation; never say sorry for 'confusion' that did not happen. "
    "Speech recognition on phone lines makes mistakes: if the caller's words are unclear, do not guess; ask them once to repeat. "
    "When everything is done and the caller says thank you or goodbye, answer with one short goodbye and nothing else, "
    "and never ask further questions after that."
)

# For an agent that hears with Deepgram or speaks with ElevenLabs: the same conversation protocol without the
# India-specific register, scripts and units. The voice says whatever the reply is written in, in its normal script.
INTERNATIONAL_HUMAN_VOICE_RULES = (
    "\n\nSound like a real person on the phone, not a script or an assistant. "
    "Talk like a warm, relaxed receptionist: everyday words, short sentences, one idea at a time. "
    "Start by reacting to what they just said in a few words when it fits, but never open two replies in a row the "
    "same way, and skip the reaction when the answer is already one short sentence. "
    "Use natural contractions where the language has them. "
    "Never say or write stock phrases like: 'I understand your concern', 'certainly', 'absolutely', 'I apologize for "
    "the inconvenience', 'kindly', 'assist', 'how may I assist you', 'feel free to', 'is there anything else I can "
    "help you with today'. Say it the way a neighbour would: 'Anything else?'. "
    "Do not repeat back what the caller just told you, do not summarise, and do not thank them in every reply. "
    "If you don't know something, say so plainly and say who will find out, instead of a long apology. "
    "Say times, prices and dates the way people say them aloud in the caller's language and local style, never as "
    "digits and symbols. "
    "A very short answer is often the most natural one. "
    "Be honest about what you are: you are the business's AI assistant. If the caller asks whether you are a robot, "
    "an AI, a machine or a real person, say plainly and warmly that you are an AI assistant, and offer to connect "
    "them to a person if they would prefer. Never claim to be human."
)


def international_conversation_rules(language_name: str) -> str:
    return (
        "\n\nConversation rules: "
        f"Start the call in {language_name}. Callers may speak any language. "
        "Reply in the language the caller used in their latest message, written in that language's normal script and "
        "spelling, because the voice reads your words aloud as written. "
        "Do not switch language on your own: if the caller's latest message is in one language, reply only in it. "
        "Greet only once, at the start; after that never say welcome or the business name again. "
        "Collect what you need in this order, one question per turn: what they want, the day and time, "
        "then confirm the contact number. Never ask about the phone number before you know the service and the time. "
        "Never ask anything you already know from the conversation, such as their name or the number they are calling from. "
        "Never offer to have someone else call, or to call another number, unless the caller asks for it. "
        "Never read out ticket numbers, reference ids or record ids; just say the request is noted. "
        "Only apologise if something actually went wrong in this conversation. "
        "Speech recognition on phone lines makes mistakes: if the caller's words are unclear, do not guess; ask them once to repeat. "
        "When everything is done and the caller says thank you or goodbye, answer with one short goodbye and nothing else, "
        "and never ask further questions after that."
    )


def voice_rules_for(agent) -> str:
    """The phone-call rules appended to an agent's prompt. Indian-market agents (Sarvam hears and speaks) get the
    India-tuned set; an agent on Deepgram or ElevenLabs gets the international one."""
    if (getattr(agent, "stt_provider", "sarvam") or "sarvam") == "sarvam" and (
        getattr(agent, "tts_provider", "sarvam") or "sarvam"
    ) == "sarvam":
        return VOICE_STYLE_RULES + HUMAN_VOICE_RULES + gender_note(agent.voice) + CONVERSATION_RULES
    from app.integrations.providers import ALL_LANGUAGES

    return (
        VOICE_STYLE_RULES
        + INTERNATIONAL_HUMAN_VOICE_RULES
        + international_conversation_rules(ALL_LANGUAGES.get(agent.language, "English"))
    )


# WhatsApp brings messages the model can't open. They arrive as a short label.
WHATSAPP_RULES = (
    "\n\nWhatsApp: keep replies short and friendly, like a chat message, with no markdown headings. "
    "A message that starts with [Voice note] is the customer's voice message, already written out for you: "
    "answer it like any other message. [Photo], [Video], [Document], [Sticker], [Location] or [Contact card] "
    "means they sent something you cannot open: thank them, say a team member will check it, and ask if "
    "there is anything you can help with meanwhile. Never pretend to have seen an attachment."
)



# Unicode script name -> (Bulbul-or-Saaras code, name). Shared scripts are
# narrowed by the agent's language or by the code Saaras detected.
_SCRIPT_LANGUAGE = {
    "DEVANAGARI": ("hi-IN", "Hindi"),
    "BENGALI": ("bn-IN", "Bengali"),
    "GUJARATI": ("gu-IN", "Gujarati"),
    "GURMUKHI": ("pa-IN", "Punjabi"),
    "ORIYA": ("od-IN", "Odia"),
    "TAMIL": ("ta-IN", "Tamil"),
    "TELUGU": ("te-IN", "Telugu"),
    "KANNADA": ("kn-IN", "Kannada"),
    "MALAYALAM": ("ml-IN", "Malayalam"),
    "ARABIC": ("ur-IN", "Urdu"),
    "OL": ("sat-IN", "Santali"),
    "MEETEI": ("mni-IN", "Manipuri"),
}
_DEVANAGARI_AGENT = {
    "mr-IN": "Marathi",
    "ne-IN": "Nepali",
    "kok-IN": "Konkani",
    "sa-IN": "Sanskrit",
    "mai-IN": "Maithili",
    "doi-IN": "Dogri",
    "brx-IN": "Bodo",
    "ks-IN": "Kashmiri",
}
_BENGALI_AGENT = {"as-IN": "Assamese", "mni-IN": "Manipuri"}
_ARABIC_AGENT = {"ur-IN": "Urdu", "ks-IN": "Kashmiri", "sd-IN": "Sindhi"}


def _script_matches(code: str, script: str) -> bool:
    """True when `code` is written in `script`. Kashmiri and Manipuri use two."""
    if code == "mni-IN" and script in ("BENGALI", "MEETEI"):
        return True
    if code == "ks-IN" and script in ("DEVANAGARI", "ARABIC"):
        return True
    families = {
        "DEVANAGARI": _DEVANAGARI_AGENT,
        "BENGALI": _BENGALI_AGENT,
        "ARABIC": _ARABIC_AGENT,
    }
    if code in families.get(script, {}):
        return True
    found = _SCRIPT_LANGUAGE.get(script)
    return found is not None and found[0] == code


def reply_language_hint(
    text: str,
    agent_language: str | None = None,
    caller_language: str | None = None,
    *,
    spoken: bool = True,
) -> str:
    """A note placed right after the caller's message: the fast model follows a
    reply-language instruction there far more reliably than one in the long prompt
    (it answered an English caller in Hindi). Saaras's detected code wins when it
    matches the script; otherwise the script, then the agent's language, decides."""
    if caller_language not in SARVAM_LANGUAGES:
        caller_language = None
    counts: dict[str, int] = {}
    latin = 0
    for ch in text:
        if not ch.isalpha():
            continue
        script = unicodedata.name(ch, "").split(" ")[0]
        if script == "LATIN":
            latin += 1
        elif script in _SCRIPT_LANGUAGE:
            counts[script] = counts.get(script, 0) + 1
    if not latin and not counts:
        return ""
    script, n = max(counts.items(), key=lambda kv: kv[1]) if counts else ("", 0)
    if latin >= n:
        return (
            "The caller's latest message is written in English letters. Reply in English letters only "
            "(plain English, or Hinglish in English letters if they mixed in Hindi words); do not use Devanagari."
        )
    code, language = _SCRIPT_LANGUAGE[script]
    if caller_language and _script_matches(caller_language, script):
        code, language = caller_language, SARVAM_LANGUAGES[caller_language]
    elif script == "DEVANAGARI" and agent_language in _DEVANAGARI_AGENT:
        code, language = agent_language, _DEVANAGARI_AGENT[agent_language]
    elif script == "BENGALI" and agent_language in _BENGALI_AGENT:
        code, language = agent_language, _BENGALI_AGENT[agent_language]
    elif script == "ARABIC" and agent_language in _ARABIC_AGENT:
        code, language = agent_language, _ARABIC_AGENT[agent_language]
    if not spoken or code in SARVAM_TTS_LANGUAGES:
        how = "its own script"
    elif code in _BENGALI_AGENT:
        how = "Bengali script"
    else:
        how = "Devanagari, so the phone voice can say it"
    return f"The caller's latest message is in {language}. Reply in {language}, in {how}."


def with_language_hint(
    messages: list[dict],
    agent_language: str | None = None,
    caller_language: str | None = None,
    *,
    spoken: bool = True,
) -> list[dict]:
    last = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    hint = reply_language_hint(
        str((last or {}).get("content") or ""), agent_language, caller_language, spoken=spoken
    )
    return [*messages, {"role": "system", "content": hint}] if hint else messages


def has_real_name(name: str | None, phone: str | None) -> bool:
    """A caller's record is created from the number alone, with the number as its name."""
    return bool(name and name.strip() and name.strip() != (phone or "").strip())


def caller_context(phone: str | None, name: str | None, *, voice: bool = True) -> str:
    """Tells the agent who is calling. Every inbound caller already has a customer
    record (created from the calling number), so the agent must never send them
    through an "account" flow or ask for their number just to look them up."""
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    who = (
        f"Their saved name is {name.strip()}; greet them by name when it fits."
        if has_real_name(name, phone)
        else "This is a new caller and you don't know their name yet: ask for it once, naturally, "
        "and pass it as caller_name whenever you create a ticket or schedule a callback. A name is not "
        "required: if they don't give it, go ahead and book without it."
    )
    return (
        f"\n\nCaller details: the caller is {'phoning' if voice else 'messaging'} from {phone} (the number ends in {digits[-4:]}). {who} "
        "Every caller's record is saved automatically from the number they call from. So never ask them to "
        "create an account, register or sign up, never say you couldn't find their account, and don't ask for "
        "their phone number just to look them up. Once you know what they want and when, and before you create a "
        f"booking or schedule a callback, ask once whether to use the number they're calling from (ending in {digits[-4:]}). "
        "If they say yes, or already said to use this number, do not ask again. Only if they give a different "
        "number, pass it as contact_phone."
    )


# Tools that change something. Conversation history only keeps what was said, so
# without this list the model can't tell it already booked and books again.
ACTION_TOOL_LABELS = {
    "create_ticket": "Created a ticket",
    "schedule_callback": "Scheduled a callback",
    "add_customer_note": "Added a note",
    "send_whatsapp_message": "Sent a WhatsApp message",
    "create_customer_payment_link": "Created a payment link",
}


def describe_action(name: str, arguments: dict | None) -> str | None:
    label = ACTION_TOOL_LABELS.get(name)
    if not label:
        return None
    a = arguments or {}
    detail = a.get("subject") or a.get("requested_time") or (f"in {a.get('minutes_from_now')} minutes" if a.get("minutes_from_now") else "") or a.get("note") or a.get("message") or a.get("amount") or ""
    return f"{label}: {str(detail)[:140]}" if detail else label


def mission_note(mission: dict | None) -> str:
    """For a call the business placed on purpose (a campaign): why we are calling and what to find out."""
    if not mission:
        return ""
    goal = str(mission.get("goal") or "").strip()
    questions = [str(q).strip() for q in (mission.get("questions") or []) if str(q).strip()]
    if not goal and not questions:
        return ""
    note = "\n\nYOU PLACED THIS CALL. Say who you are and why you are calling in your first sentence."
    if goal:
        note += f"\nReason for the call: {goal}"
    if questions:
        note += (
            "\nBy the end of the call, find out the answers to these, one question at a time, "
            "in a natural order, without sounding like a form. Skip one the person already answered:\n"
            + "\n".join(f"- {q}" for q in questions)
        )
        note += "\nThen confirm the key answers back to them in one short sentence and thank them."
    note += "\nIf they say it is a bad time, offer to call back and ask when."
    return note


def actions_note(actions: list[str]) -> str:
    if not actions:
        return ""
    listed = "\n".join(f"- {a}" for a in actions[-8:])
    return (
        "\n\nAlready done in this conversation (do not do these again; only create a new one if the caller "
        f"asks for something different):\n{listed}"
    )


def history_note(past_calls: list[tuple[datetime, str]], open_tickets: list[str]) -> str:
    """What the business already knows about this caller, so the agent can pick up
    where the last conversation ended instead of starting from zero."""
    if not past_calls and not open_tickets:
        return ""
    lines = ["\n\nWhat you already know about this caller (from earlier contact; use it naturally, "
             "never read it out as a list, and only mention it when it is relevant to what they say):"]
    for when, summary in past_calls:
        lines.append(f"- Earlier call on {when:%d %b %Y}: {summary}")
    for subject in open_tickets:
        lines.append(f"- Open request still being handled: {subject}")
    return "\n".join(lines)


async def _customer_history(db: AsyncSession, conversation: Conversation) -> str:
    if not conversation.customer_id:
        return ""
    call_query = select(Call.started_at, Call.created_at, Call.summary).where(
        Call.tenant_id == conversation.tenant_id,
        Call.customer_id == conversation.customer_id,
        Call.status == CallStatus.COMPLETED,
        Call.summary.is_not(None),
    )
    if conversation.call_id:
        call_query = call_query.where(Call.id != conversation.call_id)
    calls = (await db.execute(call_query.order_by(Call.created_at.desc()).limit(3))).all()
    tickets = (
        await db.execute(
            select(Ticket.subject)
            .where(
                Ticket.tenant_id == conversation.tenant_id,
                Ticket.customer_id == conversation.customer_id,
                Ticket.status.in_([TicketStatus.OPEN, TicketStatus.PENDING]),
                Ticket.created_at < conversation.created_at,
            )
            .order_by(Ticket.created_at.desc())
            .limit(3)
        )
    ).scalars().all()
    return history_note([(c.started_at or c.created_at, c.summary) for c in calls], list(tickets))


async def _booking_context(db: AsyncSession, conversation: Conversation) -> str:
    """Upcoming bookings up front, so "do I have a booking?" needs no lookup."""
    if not await has_bookable_services(db, conversation.tenant_id):
        return ""
    note = await today_note(db, conversation.tenant_id) + BOOKING_RULES
    if conversation.customer_id:
        note += bookings_note(await customer_bookings(db, conversation.tenant_id, conversation.customer_id))
    return note


ACTION_LOOKBACK = timedelta(hours=6)


async def _actions_this_conversation(db: AsyncSession, conversation: Conversation) -> list[str]:
    """Tickets opened for this customer since the conversation started (callbacks open
    one too). Capped to the last few hours so a long-lived WhatsApp thread stays short."""
    if not conversation.customer_id:
        return []
    since = max(conversation.created_at, datetime.now(UTC) - ACTION_LOOKBACK)
    result = await db.execute(
        select(Ticket.subject)
        .where(
            Ticket.tenant_id == conversation.tenant_id,
            Ticket.customer_id == conversation.customer_id,
            Ticket.created_at >= since,
        )
        .order_by(Ticket.created_at)
        .limit(8)
    )
    return [f"Created a ticket: {subject}" for subject in result.scalars().all()]


ASSISTANT_SYSTEM_PROMPT = (
    "You are the internal AI assistant for this company's staff. You can look up "
    "customers, tickets, and notes using the tools available to you, and you can "
    "also just answer general questions, draft text, or help think something "
    "through — like a general-purpose assistant. If a question has nothing to do "
    "with this business's data, just answer it directly without trying to force "
    "a tool call. When staff ask what is open, urgent, or waiting, call "
    "list_open_tickets (pass priority only if they named one) and answer from "
    "those rows, including each customer's name. Never say you cannot see tickets."
)

STAFF_MODEL_UNAVAILABLE = (
    "I could not reach the language model. "
    "Connect your Sarvam key under Integrations, then ask again."
)
MODEL_TROUBLE = "Sorry, I'm having trouble right now — let me get a human to help you."


@dataclass
class TurnResult:
    reply_text: str
    transfer_requested: bool = False
    transfer_reason: str | None = None
    reply_message_id: uuid.UUID | None = None


async def _persist_message(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    direction: MessageDirection,
    sender: MessageSender,
    text: str,
) -> Message:
    # created_at is set explicitly: the column's server default is now(), which
    # in Postgres is the *transaction start* time. A turn saves the caller's
    # message and the AI reply in one transaction, so both got an identical
    # timestamp and their order was arbitrary — scrambling the transcript and,
    # worse, the history the LLM reads on the next turn (ordered by created_at).
    message = Message(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        direction=direction,
        sender=sender,
        text=text,
        created_at=datetime.now(UTC),
    )
    db.add(message)
    await db.flush()
    return message


async def _enabled_tools_for_agent(db: AsyncSession, agent_id: uuid.UUID) -> list[dict]:
    result = await db.execute(select(AIAgentTool).where(AIAgentTool.agent_id == agent_id))
    rows = result.scalars().all()
    if rows:
        enabled_names = {row.tool_name for row in rows if row.enabled}
    else:
        enabled_names = set(DEFAULT_AGENT_TOOLS)
    # A business that has set up its services books into the real calendar
    # (reminders, staff view, every channel) instead of opening tickets.
    agent = await db.get(AIAgent, agent_id)
    if agent is not None and await has_bookable_services(db, agent.tenant_id):
        enabled_names |= BOOKING_TOOLS if not rows else {"list_my_appointments"}
    return [spec.as_llm_tool() for name, spec in TOOL_REGISTRY.items() if name in enabled_names]


async def _recent_history(db: AsyncSession, conversation_id: uuid.UUID) -> list[dict]:
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc())
        .limit(MAX_HISTORY_MESSAGES)
    )
    messages = list(reversed(result.scalars().all()))
    role_by_sender = {
        MessageSender.CUSTOMER: "user",
        MessageSender.AI_AGENT: "assistant",
        MessageSender.HUMAN_AGENT: "assistant",
        MessageSender.STAFF_USER: "user",
    }
    return [{"role": role_by_sender[m.sender], "content": m.text or ""} for m in messages]


ToolExecutor = Callable[[str, ToolContext, dict], Awaitable[dict]]
EventHook = Callable[[dict], None]


async def _run_tool_loop(
    *,
    messages: list[dict],
    llm_tools: list[dict],
    tool_ctx: ToolContext,
    sarvam_api_key: "str | LlmEndpoint | None",
    executor: ToolExecutor = execute_tool,
    on_event: EventHook | None = None,
    reasoning_effort: str | None = None,
) -> tuple[str, bool, str | None]:
    """Runs the chat-completion / tool-call loop to a final answer.

    Returns (reply_text, transfer_requested, transfer_reason). `messages` is
    mutated in place with the assistant/tool turns, mirroring the shape the
    LLM API expects on the next call.

    `executor` and `on_event` exist for the Flow Studio test engine
    (app/services/test_engine.py): it swaps in simulated tools and records each
    model call / tool call for the test timeline. Live calls use the defaults.
    """
    transfer_requested = False
    transfer_reason: str | None = None
    reply_text = MODEL_TROUBLE

    try:
        for _ in range(MAX_TOOL_ITERATIONS):
            started = time.perf_counter()
            completion = await chat_completion(
                messages=messages,
                tools=llm_tools or None,
                api_key=sarvam_api_key,
                reasoning_effort=reasoning_effort,
            )
            if on_event:
                on_event(
                    {
                        "type": "llm",
                        "ms": round((time.perf_counter() - started) * 1000),
                        "usage": completion.get("usage") or {},
                    }
                )
            choice = completion["choices"][0]["message"]
            tool_calls = choice.get("tool_calls") or []

            if not tool_calls:
                reply_text = choice.get("content") or ""
                break

            messages.append(choice)
            for call in tool_calls:
                function = call["function"]
                arguments = json.loads(function.get("arguments") or "{}")
                tool_started = time.perf_counter()
                result = await executor(function["name"], tool_ctx, arguments)
                if on_event:
                    on_event(
                        {
                            "type": "tool",
                            "name": function["name"],
                            "arguments": arguments,
                            "result": result,
                            "ms": round((time.perf_counter() - tool_started) * 1000),
                        }
                    )
                logger.info(
                    "tool %s args=%s -> %s",
                    function["name"],
                    sorted(arguments),
                    "error: " + str(result["error"]) if "error" in result else "ok",
                )
                if function["name"] == "transfer_call" and result.get("transfer_requested"):
                    transfer_requested = True
                    transfer_reason = result.get("reason")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps(result),
                    }
                )
        else:
            reply_text = "Let me connect you with a colleague who can take it from here."
            transfer_requested = True
            transfer_reason = "tool-call loop exceeded max iterations"
    except SarvamAPIError as exc:
        logger.warning("language model call failed: %s", exc)
        raise

    return reply_text, transfer_requested, transfer_reason


@dataclass
class _TurnPrep:
    messages: list[dict]
    llm_tools: list[dict]
    tool_ctx: ToolContext
    sarvam_api_key: "str | LlmEndpoint | None"
    guardrail_rules: list
    is_voice: bool


async def _time_note(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    """The AI has no clock: tell it the date, time and zone so "in 5 minutes" and "tomorrow at 4" work."""
    from app.services import callbacks

    tenant = await db.get(Tenant, tenant_id)
    tz = callbacks.tz_for(tenant.timezone if tenant else None)
    now = datetime.now(tz)
    return (
        f"\n\nIt is now {now.strftime('%A %d %B %Y, %I:%M %p')} ({tz.key}). "
        "If the caller asks you to call back later, use the schedule_callback tool with the time, "
        "then say the time back to them."
    )


async def _mission_for(db: AsyncSession, conversation: Conversation) -> str:
    if conversation.call_id is None:
        return ""
    call = await db.get(Call, conversation.call_id)
    return mission_note(call.mission) if call is not None else ""


async def _prepare_turn(
    *,
    db: AsyncSession,
    tenant_id: uuid.UUID,
    agent: AIAgent,
    conversation: Conversation,
    customer_text: str,
    extra_system: str = "",
    caller_language: str | None = None,
) -> _TurnPrep:
    """Saves the customer's message and builds everything the LLM call needs."""
    await _persist_message(
        db,
        tenant_id,
        conversation.id,
        direction=MessageDirection.INBOUND,
        sender=MessageSender.CUSTOMER,
        text=customer_text,
    )

    history = await _recent_history(db, conversation.id)
    llm_tools = await _enabled_tools_for_agent(db, agent.id)
    sarvam_api_key = await get_llm_key(db, tenant_id)
    is_voice = conversation.channel == ConversationChannel.VOICE
    customer = await db.get(Customer, conversation.customer_id) if conversation.customer_id else None
    system_prompt = (
        agent.system_prompt
        + TOOL_HONESTY_RULES
        + (voice_rules_for(agent) if is_voice else "")
        + (WHATSAPP_RULES if conversation.channel == ConversationChannel.WHATSAPP else "")
        + (caller_context(customer.phone, customer.name, voice=is_voice) if customer else "")
        + await _customer_history(db, conversation)
        + await other_channel_note(db, conversation)
        + await _booking_context(db, conversation)
        + actions_note(await _actions_this_conversation(db, conversation))
        + await _mission_for(db, conversation)
        + await _time_note(db, tenant_id)
    )
    guardrail_rules = await guardrails.load_rules(db, tenant_id, agent.id)
    system_prompt += guardrails.build_guardrail_prompt(guardrail_rules)
    system_prompt += extra_system
    messages: list[dict] = [{"role": "system", "content": system_prompt}, *history]
    if is_voice or conversation.channel == ConversationChannel.WHATSAPP:
        messages = with_language_hint(
            messages, agent.language, caller_language, spoken=is_voice
        )

    tool_ctx = ToolContext(
        db=db,
        tenant_id=tenant_id,
        agent_id=agent.id,
        call_id=conversation.call_id,
        conversation_id=conversation.id,
        customer_id=conversation.customer_id,
    )
    return _TurnPrep(messages, llm_tools, tool_ctx, sarvam_api_key, guardrail_rules, is_voice)


async def _finish_buffered(
    *,
    db: AsyncSession,
    tenant_id: uuid.UUID,
    agent: AIAgent,
    conversation: Conversation,
    customer_text: str,
    prep: _TurnPrep,
) -> TurnResult:
    messages, guardrail_rules, is_voice = prep.messages, prep.guardrail_rules, prep.is_voice
    try:
        reply_text, transfer_requested, transfer_reason = await _run_tool_loop(
            messages=messages,
            llm_tools=prep.llm_tools,
            tool_ctx=prep.tool_ctx,
            sarvam_api_key=prep.sarvam_api_key,
            # "low" = Sarvam's fast conversational model (~0.4s a reply). The reasoning model takes 8-10s,
            # far too slow for a customer waiting in a chat window. It stays as the automatic fallback.
            reasoning_effort="low" if (is_voice or get_settings().fast_model_for_text) else None,
        )
    except SarvamAPIError:
        # A live caller still hears one short line. Test Studio does not: it reports the real failure.
        reply_text, transfer_requested, transfer_reason = MODEL_TROUBLE, False, None
    if is_voice:
        reply_text = clean_for_speech(reply_text) or "Sorry, could you say that again?"

    if guardrail_rules:
        verdict = guardrails.check_reply(
            guardrail_rules,
            customer_text,
            reply_text,
            grounding_text="\n".join(
                str(m.get("content") or "") for m in messages if m.get("role") == "tool"
            ),
            transfer_requested=transfer_requested,
        )
        if not verdict.ok:
            reply_text = verdict.reply_text
            transfer_requested = verdict.transfer_requested
            transfer_reason = (verdict.transfer_reason or transfer_reason) if transfer_requested else None
            await guardrails.record_events(
                db,
                tenant_id=tenant_id,
                verdict=verdict,
                conversation_id=conversation.id,
                call_id=conversation.call_id,
            )

    reply_message = await _persist_message(
        db,
        tenant_id,
        conversation.id,
        direction=MessageDirection.OUTBOUND,
        sender=MessageSender.AI_AGENT,
        text=reply_text,
    )

    return TurnResult(
        reply_text=reply_text,
        transfer_requested=transfer_requested,
        transfer_reason=transfer_reason,
        reply_message_id=reply_message.id,
    )


async def run_turn(
    *,
    db: AsyncSession,
    tenant_id: uuid.UUID,
    agent: AIAgent,
    conversation: Conversation,
    customer_text: str,
    extra_system: str = "",
    caller_language: str | None = None,
) -> TurnResult:
    prep = await _prepare_turn(
        db=db,
        tenant_id=tenant_id,
        agent=agent,
        conversation=conversation,
        customer_text=customer_text,
        extra_system=extra_system,
        caller_language=caller_language,
    )
    return await _finish_buffered(
        db=db,
        tenant_id=tenant_id,
        agent=agent,
        conversation=conversation,
        customer_text=customer_text,
        prep=prep,
    )


async def _run_tool_loop_stream(
    *,
    messages: list[dict],
    llm_tools: list[dict],
    tool_ctx: ToolContext,
    sarvam_api_key: "str | LlmEndpoint | None",
    reasoning_effort: str | None,
) -> AsyncIterator[tuple]:
    """Streaming twin of _run_tool_loop for live calls. Yields ("sentence", text)
    the moment each sentence of the reply is complete, then one
    ("done", reply_text, transfer_requested, transfer_reason).

    reply_text is exactly what was streamed out (so the stored transcript is what
    the caller heard), including any sentence the model said before calling a
    tool. If the model fails after words were already spoken, the call carries
    on with what was said rather than an apology on top of it."""
    transfer_requested = False
    transfer_reason: str | None = None
    spoken: list[str] = []
    fallback = "Sorry, I'm having trouble right now — let me get a human to help you."
    reply_text: str | None = None

    try:
        for _ in range(MAX_TOOL_ITERATIONS):
            streamer = SentenceStreamer()
            final: dict = {}
            async for event in chat_completion_stream(
                messages=messages,
                tools=llm_tools or None,
                api_key=sarvam_api_key,
                reasoning_effort=reasoning_effort,
            ):
                if event["type"] == "content":
                    for sentence in streamer.feed(event["text"]):
                        spoken.append(sentence)
                        yield ("sentence", sentence)
                else:
                    final = event
            for sentence in streamer.flush():
                spoken.append(sentence)
                yield ("sentence", sentence)

            tool_calls = final.get("tool_calls") or []
            if not tool_calls:
                reply_text = " ".join(spoken)
                break

            messages.append(
                {"role": "assistant", "content": final.get("content") or None, "tool_calls": tool_calls}
            )
            for call in tool_calls:
                function = call["function"]
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except ValueError:
                    arguments = {}
                result = await execute_tool(function["name"], tool_ctx, arguments)
                logger.info(
                    "tool %s args=%s -> %s",
                    function["name"],
                    sorted(arguments),
                    "error: " + str(result["error"]) if "error" in result else "ok",
                )
                if function["name"] == "transfer_call" and result.get("transfer_requested"):
                    transfer_requested = True
                    transfer_reason = result.get("reason")
                messages.append(
                    {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)}
                )
        else:
            closing = "Let me connect you with a colleague who can take it from here."
            spoken.append(closing)
            yield ("sentence", closing)
            reply_text = " ".join(spoken)
            transfer_requested = True
            transfer_reason = "tool-call loop exceeded max iterations"
    except SarvamAPIError:
        logger.warning("streaming turn failed", exc_info=True)
        if not spoken:
            spoken.append(fallback)
            yield ("sentence", fallback)
        reply_text = " ".join(spoken)

    yield ("done", reply_text or fallback, transfer_requested, transfer_reason)


async def run_turn_stream(
    *,
    db: AsyncSession,
    tenant_id: uuid.UUID,
    agent: AIAgent,
    conversation: Conversation,
    customer_text: str,
    caller_language: str | None = None,
) -> AsyncIterator[tuple]:
    """Voice-call turn that streams: yields ("sentence", text) as the reply is
    spoken-ready, then ("result", TurnResult). Agents with guardrail rules are
    not streamed: a rule can veto the finished reply, which can't be taken back
    once it has been spoken, so they get the whole checked reply at the end."""
    prep = await _prepare_turn(
        db=db,
        tenant_id=tenant_id,
        agent=agent,
        conversation=conversation,
        customer_text=customer_text,
        caller_language=caller_language,
    )
    if prep.guardrail_rules or not prep.is_voice:
        result = await _finish_buffered(
            db=db,
            tenant_id=tenant_id,
            agent=agent,
            conversation=conversation,
            customer_text=customer_text,
            prep=prep,
        )
        yield ("result", result)
        return

    reply_text, transfer_requested, transfer_reason = "", False, None
    async for event in _run_tool_loop_stream(
        messages=prep.messages,
        llm_tools=prep.llm_tools,
        tool_ctx=prep.tool_ctx,
        sarvam_api_key=prep.sarvam_api_key,
        reasoning_effort="low",
    ):
        if event[0] == "sentence":
            yield event
        else:
            _, reply_text, transfer_requested, transfer_reason = event
    reply_text = clean_for_speech(reply_text) or "Sorry, could you say that again?"
    reply_message = await _persist_message(
        db,
        tenant_id,
        conversation.id,
        direction=MessageDirection.OUTBOUND,
        sender=MessageSender.AI_AGENT,
        text=reply_text,
    )
    yield (
        "result",
        TurnResult(
            reply_text=reply_text,
            transfer_requested=transfer_requested,
            transfer_reason=transfer_reason,
            reply_message_id=reply_message.id,
        ),
    )


async def run_assistant_turn(
    *,
    db: AsyncSession,
    tenant_id: uuid.UUID,
    conversation: Conversation,
    user_text: str,
) -> str:
    """The staff-facing counterpart to `run_turn` — not tied to any `AIAgent`
    row, uses a fixed built-in prompt/tool set instead of a tenant-configured
    one. See module docstring."""
    await _persist_message(
        db,
        tenant_id,
        conversation.id,
        direction=MessageDirection.INBOUND,
        sender=MessageSender.STAFF_USER,
        text=user_text,
    )

    history = await _recent_history(db, conversation.id)
    llm_tools = [
        TOOL_REGISTRY[name].as_llm_tool() for name in ASSISTANT_TOOL_NAMES if name in TOOL_REGISTRY
    ]
    sarvam_api_key = await get_llm_key(db, tenant_id)
    messages: list[dict] = [{"role": "system", "content": ASSISTANT_SYSTEM_PROMPT}, *history]

    tool_ctx = ToolContext(
        db=db,
        tenant_id=tenant_id,
        agent_id=None,
        call_id=None,
        conversation_id=conversation.id,
        customer_id=None,
    )

    try:
        reply_text, _transfer_requested, _transfer_reason = await _run_tool_loop(
            messages=messages, llm_tools=llm_tools, tool_ctx=tool_ctx, sarvam_api_key=sarvam_api_key
        )
    except SarvamAPIError:
        reply_text = STAFF_MODEL_UNAVAILABLE

    await _persist_message(
        db,
        tenant_id,
        conversation.id,
        direction=MessageDirection.OUTBOUND,
        sender=MessageSender.AI_AGENT,
        text=reply_text,
    )

    return reply_text
