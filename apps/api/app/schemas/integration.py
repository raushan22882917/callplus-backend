import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from packages.database.models import IntegrationStatus


class IntegrationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider: str
    status: IntegrationStatus
    config: dict
    created_at: datetime


class ConnectTwilioRequest(BaseModel):
    account_sid: str = Field(min_length=1, max_length=64)
    auth_token: str = Field(min_length=1)


class ConnectSarvamRequest(BaseModel):
    api_key: str = Field(min_length=1)
