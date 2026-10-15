import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RuleKind = Literal[
    "never_reveal",
    "never_invent_price",
    "no_refund_without_auth",
    "restricted_topic",
    "escalate_keyword",
    "escalate_on_anger",
    "escalate_low_confidence",
    "custom_instruction",
    "max_discount_percent",
    "working_hours",
]

_HHMM = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
_KEYWORD_KINDS = {"restricted_topic", "escalate_keyword"}
_TEXT_KINDS = {"never_reveal", "custom_instruction"}


def validate_rule(kind: str, text: str | None, params: dict[str, Any]) -> None:
    """Raises ValueError when the rule can't do anything useful for its kind."""
    has_keywords = bool(params.get("keywords")) or bool((text or "").strip())
    if kind in _KEYWORD_KINDS and not has_keywords:
        raise ValueError("this rule needs keywords (params.keywords or comma-separated text)")
    if kind in _TEXT_KINDS and not (text or "").strip():
        raise ValueError("this rule needs text")
    if kind == "max_discount_percent":
        try:
            pct = float(params.get("percent"))
        except (TypeError, ValueError):
            raise ValueError("params.percent is required (0-100)") from None
        if not 0 <= pct <= 100:
            raise ValueError("params.percent must be between 0 and 100")
    if kind == "working_hours":
        for key in ("start", "end"):
            if key in params and not _HHMM.match(str(params[key])):
                raise ValueError(f"params.{key} must be HH:MM")
        days = params.get("days")
        if days is not None and (
            not isinstance(days, list) or any(not isinstance(d, int) or not 0 <= d <= 6 for d in days)
        ):
            raise ValueError("params.days must be a list of 0 (Mon) to 6 (Sun)")
        tz = params.get("timezone")
        if tz is not None:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            try:
                ZoneInfo(str(tz))
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("params.timezone is not a valid IANA timezone") from None


class GuardrailRuleCreate(BaseModel):
    kind: RuleKind
    ai_agent_id: uuid.UUID | None = None
    text: str | None = Field(default=None, max_length=4000)
    params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    priority: int = Field(default=100, ge=0, le=10_000)

    @model_validator(mode="after")
    def _check(self) -> "GuardrailRuleCreate":
        validate_rule(self.kind, self.text, self.params)
        return self


class GuardrailRuleUpdate(BaseModel):
    kind: RuleKind | None = None
    ai_agent_id: uuid.UUID | None = None
    text: str | None = Field(default=None, max_length=4000)
    params: dict[str, Any] | None = None
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=10_000)


class GuardrailRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ai_agent_id: uuid.UUID | None
    kind: str
    text: str | None
    params: dict[str, Any]
    enabled: bool
    priority: int
    created_at: datetime
    updated_at: datetime


class GuardrailEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    call_id: uuid.UUID | None
    conversation_id: uuid.UUID | None
    rule_id: uuid.UUID | None
    rule_kind: str
    action: str
    excerpt: str
    created_at: datetime


class GuardrailTemplate(BaseModel):
    key: str
    title: str
    description: str
    kind: RuleKind
    text: str | None
    params: dict[str, Any]
    priority: int


class KnowledgeControlUpdate(BaseModel):
    enabled: bool
    visibility: Literal["customer", "internal"] = "customer"


class KnowledgeControlRead(BaseModel):
    document_id: uuid.UUID
    title: str
    chunk_count: int
    enabled: bool
    visibility: str
    updated_at: datetime
