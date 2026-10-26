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


class ConnectExotelRequest(BaseModel):
    account_sid: str = Field(min_length=1, max_length=64)
    api_key: str = Field(min_length=1, max_length=200)
    api_token: str = Field(min_length=1, max_length=400)
    # The region on Exotel's API Settings page: Singapore api.exotel.com, Mumbai api.in.exotel.com.
    subdomain: str = Field(default="api.exotel.com", max_length=64)


class ExotelSetup(BaseModel):
    """What the person pastes into their Exotel flow; the address is shown only when created."""

    integration: IntegrationRead
    stream_url: str
    steps: list[str]
