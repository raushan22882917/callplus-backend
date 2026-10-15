import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class WhatsAppAccountConnect(BaseModel):
    phone_number_id: str = Field(min_length=1, max_length=64)
    waba_id: str = Field(min_length=1, max_length=64)
    display_phone_number: str = Field(min_length=1, max_length=20)
    access_token: str = Field(min_length=1)


class WhatsAppAccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    phone_number_id: str
    waba_id: str
    display_phone_number: str
    ai_agent_id: uuid.UUID | None = None
    created_at: datetime


class WhatsAppAccountUpdate(BaseModel):
    ai_agent_id: uuid.UUID | None = None
