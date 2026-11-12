import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.database.models import CampaignChannel, CampaignContactStatus, CampaignStatus


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    channel: CampaignChannel
    ai_agent_id: uuid.UUID | None = None
    whatsapp_template_name: str | None = None
    email_subject: str | None = Field(default=None, max_length=200)
    email_body: str | None = Field(default=None, max_length=20000)
    email_reply_to: str | None = Field(default=None, max_length=255)
    sms_body: str | None = Field(default=None, max_length=640)
    scheduled_at: datetime | None = None
    goal: str | None = Field(default=None, max_length=2000)
    questions: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def channel_requirements(self) -> "CampaignCreate":
        if self.channel == CampaignChannel.EMAIL:
            if not (self.email_subject or "").strip() or not (self.email_body or "").strip():
                raise ValueError("An email campaign needs a subject and a message")
            reply_to = (self.email_reply_to or "").strip()
            if reply_to and "@" not in reply_to:
                raise ValueError("Reply-to must be an email address")
            self.email_subject = self.email_subject.strip()
            self.email_body = self.email_body.strip()
            self.email_reply_to = reply_to or None
        if self.channel == CampaignChannel.SMS:
            if not (self.sms_body or "").strip():
                raise ValueError("An SMS campaign needs a message")
            self.sms_body = self.sms_body.strip()
        return self


class CampaignRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    channel: CampaignChannel
    status: CampaignStatus
    ai_agent_id: uuid.UUID | None
    whatsapp_template_name: str | None
    email_subject: str | None = None
    email_body: str | None = None
    email_reply_to: str | None = None
    sms_body: str | None = None
    scheduled_at: datetime | None
    goal: str | None = None
    questions: list[str] = []
    created_at: datetime


class CampaignUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    email_subject: str | None = Field(default=None, max_length=200)
    email_body: str | None = Field(default=None, max_length=20000)
    email_reply_to: str | None = Field(default=None, max_length=255)
    sms_body: str | None = Field(default=None, max_length=640)
    scheduled_at: datetime | None = None


class CampaignEnroll(BaseModel):
    audience: Literal["with_email", "with_phone", "tag"] = "with_email"
    tags: list[str] = Field(default_factory=list, max_length=10)


class EmailDraftRequest(BaseModel):
    instruction: str = Field(default="", max_length=2000)
    current_subject: str = Field(default="", max_length=200)
    current_body: str = Field(default="", max_length=8000)


class EmailDraftResponse(BaseModel):
    subject: str
    body: str


class SmsDraftRequest(BaseModel):
    instruction: str = Field(default="", max_length=2000)
    current_body: str = Field(default="", max_length=640)


class SmsDraftResponse(BaseModel):
    body: str


class CampaignAddContacts(BaseModel):
    customer_ids: list[uuid.UUID] = Field(min_length=1, max_length=10000)


class CampaignContactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    customer_id: uuid.UUID
    status: CampaignContactStatus
    attempted_at: datetime | None
    result: dict


class PlanMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2000)


class PlanRequest(BaseModel):
    messages: list[PlanMessage] = Field(min_length=1, max_length=40)


class PlanAudience(BaseModel):
    type: Literal["all", "tag", "numbers"] = "all"
    tags: list[str] = Field(default_factory=list, max_length=10)
    numbers: list[str] = Field(default_factory=list, max_length=200)


class CampaignPlan(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    goal: str = Field(default="", max_length=1000)
    questions: list[str] = Field(default_factory=list, max_length=8)
    audience: PlanAudience = Field(default_factory=PlanAudience)
    scheduled_at: datetime | None = None


class PlanResponse(BaseModel):
    ready: bool
    reply: str
    plan: CampaignPlan | None = None


class FromPlanRequest(BaseModel):
    plan: CampaignPlan
    ai_agent_id: uuid.UUID | None = None
