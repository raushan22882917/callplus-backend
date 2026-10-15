import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from packages.database.models import CampaignChannel, CampaignContactStatus, CampaignStatus


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    channel: CampaignChannel
    ai_agent_id: uuid.UUID | None = None
    whatsapp_template_name: str | None = None
    scheduled_at: datetime | None = None


class CampaignRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    channel: CampaignChannel
    status: CampaignStatus
    ai_agent_id: uuid.UUID | None
    whatsapp_template_name: str | None
    scheduled_at: datetime | None
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
