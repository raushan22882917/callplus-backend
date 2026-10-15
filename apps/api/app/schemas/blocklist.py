import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class BlockedCallerCreate(BaseModel):
    e164_number: str = Field(min_length=8, max_length=20, pattern=r"^\+[1-9]\d{6,18}$")
    reason: str = "manual"


class BlockedCallerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    e164_number: str
    reason: str
    created_at: datetime
