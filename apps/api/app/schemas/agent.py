import uuid
from datetime import datetime
from typing import Literal

from app.integrations.sarvam import SARVAM_LANGUAGES
from pydantic import BaseModel, ConfigDict, Field, field_validator

from packages.database.models import AgentStatus


class AIAgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    language: str = "en-IN"
    voice: str | None = None
    greeting: str | None = None
    system_prompt: str = (
        "You are a helpful AI assistant for this business. Use the available "
        "tools when they help, and answer any other question the caller has "
        "as best you can."
    )
    max_call_duration_seconds: int = Field(default=300, ge=10, le=3600)
    no_response_timeout_seconds: int = Field(default=6, ge=2, le=60)
    max_no_response_retries: int = Field(default=2, ge=0, le=10)
    no_response_prompt: str = "Sorry, I didn't catch that — could you repeat it?"
    max_duration_message: str = "I'm sorry, but I need to end our call now. Thank you for calling."
    no_response_end_message: str = (
        "I'm still not hearing anything, so I'll end the call here. Feel free to call back."
    )

    @field_validator("language")
    @classmethod
    def _supported_language(cls, value: str | None) -> str | None:
        if value is not None and value not in SARVAM_LANGUAGES:
            raise ValueError(f"language must be one of: {', '.join(SARVAM_LANGUAGES)}")
        return value


class AIAgentUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    status: AgentStatus | None = None
    language: str | None = None
    voice: str | None = None
    greeting: str | None = None
    system_prompt: str | None = None
    max_call_duration_seconds: int | None = Field(default=None, ge=10, le=3600)
    no_response_timeout_seconds: int | None = Field(default=None, ge=2, le=60)
    max_no_response_retries: int | None = Field(default=None, ge=0, le=10)
    no_response_prompt: str | None = None
    max_duration_message: str | None = None
    no_response_end_message: str | None = None

    @field_validator("language")
    @classmethod
    def _supported_language(cls, value: str | None) -> str | None:
        if value is not None and value not in SARVAM_LANGUAGES:
            raise ValueError(f"language must be one of: {', '.join(SARVAM_LANGUAGES)}")
        return value


class AIAgentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    status: AgentStatus
    language: str
    voice: str | None
    greeting: str | None
    system_prompt: str
    max_call_duration_seconds: int
    no_response_timeout_seconds: int
    max_no_response_retries: int
    no_response_prompt: str
    max_duration_message: str
    no_response_end_message: str
    created_at: datetime
    updated_at: datetime


class AIAgentToolUpdate(BaseModel):
    tool_name: str
    enabled: bool = True
    configuration: dict = Field(default_factory=dict)


class AIAgentToolRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tool_name: str
    enabled: bool
    configuration: dict


class PhoneNumberCreate(BaseModel):
    e164_number: str = Field(min_length=8, max_length=20, pattern=r"^\+[1-9]\d{6,18}$")
    ai_agent_id: uuid.UUID | None = None


class PhoneNumberUpdate(BaseModel):
    """Only the agent a number routes to can change; send `ai_agent_id: null`
    to leave inbound calls unanswered (they are rejected cleanly)."""

    ai_agent_id: uuid.UUID | None = None


class PhoneNumberRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    e164_number: str
    provider: str
    ai_agent_id: uuid.UUID | None
    created_at: datetime


class AvailableNumber(BaseModel):
    phone_number: str
    friendly_name: str
    locality: str | None
    region: str | None


class PhoneNumberProvision(BaseModel):
    phone_number: str = Field(min_length=8, max_length=20, pattern=r"^\+[1-9]\d{6,18}$")
    ai_agent_id: uuid.UUID | None = None


# --- Flow Studio (visual editor over AIAgent.system_prompt) ---
#
# Nodes/edges are stored as-is in AIAgentVersion.configuration (JSONB) rather
# than modeled field-by-field — the frontend owns the exact visual shape
# (position, color, badge text, etc.) and the backend only needs a handful
# of keys (id/type/title/prompt, from/to/label) to compile a system prompt;
# see app/services/flow_compiler.py.


class AgentFlowSave(BaseModel):
    nodes: list[dict] = Field(min_length=1)
    edges: list[dict] = Field(default_factory=list)
    publish: bool = True


class AgentFlowRead(BaseModel):
    version: int
    published: bool
    nodes: list[dict]
    edges: list[dict]


class TestChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class TestChatRequest(BaseModel):
    messages: list[TestChatMessage] = Field(min_length=1, max_length=40)
    # Lets the Flow Studio "Test Agent" panel preview unsaved edits before
    # hitting Save — compiled from these instead of the agent's saved flow.
    nodes: list[dict] | None = None
    edges: list[dict] | None = None


class TestChatResponse(BaseModel):
    reply: str
