import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from packages.database.models import CampaignChannel, CampaignContactStatus, CampaignStatus


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    channel: CampaignChannel
    ai_agent_id: uuid.UUID | None = None
    whatsapp_template_name: str | None = None
    scheduled_at: datetime | None = None
    goal: str | None = Field(default=None, max_length=2000)
    questions: list[str] = Field(default_factory=list, max_length=20)


class CampaignRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    channel: CampaignChannel
    status: CampaignStatus
    ai_agent_id: uuid.UUID | None
    whatsapp_template_name: str | None
    scheduled_at: datetime | None
    goal: str | None = None
    questions: list[str] = []
    created_at: datetime


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
