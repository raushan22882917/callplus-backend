"""Pure parsers/validators for inbound channel payloads (email providers and
Twilio voicemail TwiML). No I/O, so they are unit tested directly
(tests/test_channels.py)."""

import re
from dataclasses import dataclass, field
from email import message_from_string
from email.utils import parseaddr

from twilio.twiml.voice_response import VoiceResponse

# ---------------------------------------------------------------- email


@dataclass
class InboundEmail:
    from_address: str
    from_name: str
    to_address: str
    subject: str
    text: str
    message_id: str | None
    in_reply_to: str | None
    references: list[str] = field(default_factory=list)
    is_automated: bool = False


_REPLY_MARKERS = (
    re.compile(r"^On .{5,200}wrote:\s*$", re.IGNORECASE),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^From:\s.+", re.IGNORECASE),
)


def strip_quoted_reply(text: str) -> str:
    """Drops the quoted previous message so the AI only sees what is new."""
    kept: list[str] = []
    lines = text.replace("\r\n", "\n").split("\n")
    for index, line in enumerate(lines):
        if line.startswith(">"):
            continue
        # "On <date>, X wrote:" often wraps onto two lines.
        joined = f"{line} {lines[index + 1]}".strip() if index + 1 < len(lines) else line
        if any(m.match(line) for m in _REPLY_MARKERS) or (
            re.match(r"^On .{5,200}wrote:\s*$", joined, re.IGNORECASE) and line.startswith("On ")
        ):
            break
        kept.append(line)
    return "\n".join(kept).strip()


def _first(data: dict, *keys: str) -> str:
    lowered = {str(k).lower(): v for k, v in data.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _id_tokens(value: str) -> list[str]:
    return re.findall(r"<[^<>\s]+>", value) or ([value.strip()] if value.strip() else [])


def parse_email_payload(data: dict) -> InboundEmail | None:
    """Accepts a generic JSON body or the form fields posted by Mailgun
    (`sender`, `recipient`, `body-plain`, `stripped-text`, `Message-Id`...) and
    SendGrid Inbound Parse (`from`, `to`, `text`, raw `headers`). Returns None
    when there is no usable sender or body."""
    raw_headers = _first(data, "headers", "message-headers")
    parsed_headers = {}
    if raw_headers and not raw_headers.startswith("["):
        parsed_headers = {k.lower(): v for k, v in message_from_string(raw_headers).items()}

    def header(name: str) -> str:
        return _first(data, name, name.replace("-", "_")) or str(parsed_headers.get(name, "")).strip()

    from_name, from_address = parseaddr(_first(data, "from", "sender"))
    _, to_address = parseaddr(_first(data, "to", "recipient"))
    body = _first(data, "stripped-text", "text", "body-plain", "plain", "body")
    if not from_address or not body:
        return None

    message_id = header("message-id") or None
    automated = (
        header("auto-submitted").lower() not in ("", "no")
        or header("precedence").lower() in ("bulk", "junk", "list", "auto_reply")
        or bool(header("x-autoreply"))
        or from_address.lower().startswith(("mailer-daemon@", "postmaster@", "no-reply@", "noreply@"))
    )
    return InboundEmail(
        from_address=from_address.lower(),
        from_name=from_name.strip(),
        to_address=to_address.lower(),
        subject=_first(data, "subject")[:255],
        text=strip_quoted_reply(body)[:10000] or body[:10000],
        message_id=message_id[:128] if message_id else None,
        in_reply_to=(_id_tokens(header("in-reply-to")) or [None])[0],
        references=_id_tokens(header("references")),
        is_automated=automated,
    )


def chunk_text(text: str, limit: int) -> list[str]:
    """Splits on whitespace so a long AI reply fits the channel's per-message cap."""
    text = text.strip()
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind(" ", 0, limit)
        cut = cut if cut > limit // 2 else limit
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


# ---------------------------------------------------------------- voicemail


def build_voicemail_twiml(
    *, greeting: str, recording_callback_url: str, transcribe_callback_url: str
) -> str:
    response = VoiceResponse()
    response.say(greeting)
    response.record(
        max_length=120,
        play_beep=True,
        transcribe=True,
        transcribe_callback=transcribe_callback_url,
        recording_status_callback=recording_callback_url,
        recording_status_callback_method="POST",
        recording_status_callback_event="completed",
    )
    response.say("We did not receive a message. Goodbye.")
    response.hangup()
    return str(response)
