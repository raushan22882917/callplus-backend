import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ChannelName = Literal["sms", "email", "web_chat", "instagram", "messenger", "web_form", "voicemail"]

# Keys a tenant may store per channel. Anything else is rejected so the JSON
# columns cannot be used as free-form storage.
CONFIG_KEYS: dict[str, set[str]] = {
    "sms": {"from_number"},
    "email": {"reply_to_address", "inbound_address"},
    "web_chat": {
        "title",
        "greeting",
        "color",
        "allowed_origins",
        "lead_form",
        "require_phone",
        "call_me",
        "consent_text",
        "allowed_countries",
        "max_calls_per_day",
        "template",
        "subtitle",
        "position",
        "launcher_label",
        "lead_heading",
        "lead_intro",
        "button_label",
        "show_email",
        "show_message",
        "use_knowledge",
        "kb_only",
        "privacy_consent",
        "fallback_message",
        "quick_replies",
        "form_fields",
        "call_from_number",
    },
    "instagram": set(),
    "messenger": set(),
    "web_form": {"allowed_origins", "callback_followup", "success_message", "ticket_priority"},
    "voicemail": {"greeting"},
}
CREDENTIAL_KEYS: dict[str, set[str]] = {
    "sms": set(),
    "email": {"webhook_secret"},
    "web_chat": set(),
    "instagram": {"page_access_token", "app_secret", "verify_token"},
    "messenger": {"page_access_token", "app_secret", "verify_token"},
    "web_form": set(),
    "voicemail": set(),
}


class ChannelSettingsRead(BaseModel):
    channel: str
    label: str
    enabled: bool
    public_key: str | None
    external_id: str | None
    ai_agent_id: uuid.UUID | None
    config: dict
    # Names of stored credentials; their values are never returned.
    credentials_set: list[str]
    webhook_url: str | None
    widget_snippet: str | None
    setup_notes: str


class ChannelSettingsUpdate(BaseModel):
    enabled: bool | None = None
    ai_agent_id: uuid.UUID | None = None
    clear_ai_agent: bool = False
    external_id: str | None = Field(default=None, max_length=128)
    config: dict | None = None
    # Write-only. A value of "" removes that credential.
    credentials: dict[str, str] | None = None
    rotate_public_key: bool = False


class InboxCustomer(BaseModel):
    id: uuid.UUID
    name: str
    phone: str | None
    email: str | None


class InboxLastMessage(BaseModel):
    text: str | None
    direction: str
    sender: str
    created_at: datetime


class InboxItem(BaseModel):
    id: uuid.UUID
    channel: str
    customer: InboxCustomer | None
    subject: str | None
    contact_address: str | None
    last_message: InboxLastMessage | None
    last_message_at: datetime
    unread_count: int
    created_at: datetime


class ThreadMessage(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    direction: str
    sender: str
    text: str | None
    media_url: str | None
    delivery_status: str | None = None
    delivery_error: str | None = None
    created_at: datetime


class ThreadRead(BaseModel):
    conversation: InboxItem
    messages: list[ThreadMessage]
    can_reply: bool
    reply_hint: str | None
    # Human takeover: while set (and in the future) the AI agent stays quiet.
    ai_paused_until: datetime | None = None
    # WhatsApp only: free text is allowed for 24h after the customer's last
    # message; after that only an approved template can be sent.
    whatsapp_window_open: bool | None = None


class SendTemplatePayload(BaseModel):
    template_id: uuid.UUID
    values: dict[str, str] = {}


class FollowUpPayload(BaseModel):
    """Reach the customer of a conversation on another channel (after a call)."""

    via: Literal["whatsapp", "sms", "email"]
    text: str = Field(default="", max_length=4000)
    subject: str | None = Field(default=None, max_length=200)
    template_id: uuid.UUID | None = None
    values: dict[str, str] = Field(default_factory=dict)


class FollowUpResult(BaseModel):
    conversation_id: uuid.UUID
    channel: str
    message: ThreadMessage


class AIControlPayload(BaseModel):
    active: bool


class ReplyPayload(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class ClickToCallPayload(BaseModel):
    to_number: str = Field(min_length=5, max_length=32, pattern=r"^\+[1-9]\d{4,18}$")
    mode: Literal["ai", "bridge"] = "ai"
    ai_agent_id: uuid.UUID | None = None
    from_number: str | None = Field(default=None, pattern=r"^\+[1-9]\d{4,18}$")
    agent_phone: str | None = Field(default=None, pattern=r"^\+[1-9]\d{4,18}$")


class ClickToCallRead(BaseModel):
    call_id: uuid.UUID
    status: str
    mode: str


class CallbackPayload(BaseModel):
    mode: Literal["ai", "bridge"] = "ai"
    ai_agent_id: uuid.UUID | None = None
    from_number: str | None = Field(default=None, pattern=r"^\+[1-9]\d{4,18}$")
    agent_phone: str | None = Field(default=None, pattern=r"^\+[1-9]\d{4,18}$")


class HandledPayload(BaseModel):
    handled: bool = True


class VoicemailRead(BaseModel):
    id: uuid.UUID
    missed_call_id: uuid.UUID | None
    customer_id: uuid.UUID | None
    customer_name: str | None
    from_number: str | None
    to_number: str | None
    recording_url: str | None
    duration_seconds: int | None
    transcription: str | None
    transcription_status: str
    handled: bool
    handled_at: datetime | None
    created_at: datetime


class MissedCallRead(BaseModel):
    id: uuid.UUID
    call_id: uuid.UUID | None
    customer_id: uuid.UUID | None
    customer_name: str | None
    from_number: str | None
    to_number: str | None
    reason: str
    handled: bool
    handled_at: datetime | None
    callback_call_id: uuid.UUID | None
    created_at: datetime
    voicemail: VoicemailRead | None = None
