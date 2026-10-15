import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class McpApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class McpApiKeyCreated(BaseModel):
    """Returned exactly once, at creation — `api_key` is never retrievable
    again afterwards, since only its hash is stored."""

    id: uuid.UUID
    name: str
    key_prefix: str
    api_key: str
    created_at: datetime


class McpApiKeyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    key_prefix: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
