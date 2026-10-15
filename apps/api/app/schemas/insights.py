import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ScorecardBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=500)
    is_default: bool = False
    greeting_phrases: list[str] = Field(default_factory=list, max_length=30)
    introduction_required: bool = True
    script_phrases: list[str] = Field(default_factory=list, max_length=30)
    forbidden_claims: list[str] = Field(default_factory=list, max_length=50)
    closing_phrases: list[str] = Field(default_factory=list, max_length=30)
    max_silence_seconds: int = Field(default=20, ge=3, le=300)


class ScorecardCreate(ScorecardBase):
    pass


class ScorecardUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=500)
    is_default: bool | None = None
    greeting_phrases: list[str] | None = None
    introduction_required: bool | None = None
    script_phrases: list[str] | None = None
    forbidden_claims: list[str] | None = None
    closing_phrases: list[str] | None = None
    max_silence_seconds: int | None = Field(default=None, ge=3, le=300)


class ScorecardRead(ScorecardBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class QAProblem(BaseModel):
    code: str
    severity: str
    message: str
    evidence: str | None = None
    source: str


class QAScoreRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    call_id: uuid.UUID
    scorecard_id: uuid.UUID | None
    compliance: int | None
    communication: int | None
    resolution: int | None
    sales: int | None
    overall: int
    problems: list[QAProblem]
    summary: str | None
    llm_used: bool
    scored_at: datetime


class QAScoreListItem(QAScoreRead):
    call_created_at: datetime
    direction: str
    customer_name: str | None = None
    agent_name: str | None = None
    duration_seconds: int | None = None


class RescoreRequest(BaseModel):
    scorecard_id: uuid.UUID | None = None


class QASummary(BaseModel):
    scored_calls: int
    avg_overall: float | None
    avg_compliance: float | None
    avg_communication: float | None
    avg_resolution: float | None
    avg_sales: float | None
    calls_with_critical_problems: int


class CopilotTurn(BaseModel):
    speaker: str = Field(pattern="^(agent|caller)$")
    text: str = Field(min_length=1, max_length=4000)


class CopilotSuggestRequest(BaseModel):
    caller_text: str = Field(min_length=1, max_length=4000)
    call_id: uuid.UUID | None = None
    conversation: list[CopilotTurn] = Field(default_factory=list, max_length=100)


class KnowledgeHit(BaseModel):
    document_id: str
    title: str
    content: str
    score: float


class CopilotSuggestion(BaseModel):
    suggested_reply: str
    intent: str | None
    next_action: str | None
    compliance_warning: str | None
    upsell_hint: str | None
    translation_en: str | None
    knowledge_hits: list[KnowledgeHit]


class CopilotSummarizeRequest(BaseModel):
    call_id: uuid.UUID | None = None
    conversation: list[CopilotTurn] = Field(default_factory=list, max_length=200)


class CopilotSummary(BaseModel):
    summary: str
    key_points: list[str]
    action_items: list[str]
    sentiment: str
