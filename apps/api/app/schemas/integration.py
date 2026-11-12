import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

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


class ConnectEmailSenderRequest(BaseModel):
    """The workspace's own Resend or Brevo account, and the address its customer email comes from."""

    provider: Literal["resend", "brevo"]
    api_key: str = Field(min_length=8, max_length=400)
    from_email: EmailStr
    from_name: str | None = Field(default=None, max_length=80)


class ConnectSarvamRequest(BaseModel):
    api_key: str = Field(min_length=1)


class ConnectElevenLabsRequest(BaseModel):
    api_key: str = Field(min_length=1)
    # The speaking model. Flash is the fastest, which matters on a live call.
    model: str | None = Field(default=None, max_length=80)


class ConnectDeepgramRequest(BaseModel):
    api_key: str = Field(min_length=1)


class ConnectLlmRequest(BaseModel):
    """Any service that speaks the OpenAI chat-completions format."""

    preset: str = Field(min_length=1, max_length=40)
    api_key: str = Field(min_length=1)
    model: str | None = Field(default=None, max_length=120)
    base_url: str | None = Field(default=None, max_length=300)


class SetProviderModeRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=20)
    # "managed" = CallPlus's account, "own" = the workspace's, null = no choice (own if connected, else CallPlus)
    mode: Literal["own", "managed"] | None = None


class ConnectExotelRequest(BaseModel):
    account_sid: str = Field(min_length=1, max_length=64)
    api_key: str = Field(min_length=1, max_length=200)
    api_token: str = Field(min_length=1, max_length=400)
    # The region on Exotel's API Settings page: Singapore api.exotel.com, Mumbai api.in.exotel.com.
    subdomain: str = Field(default="api.exotel.com", max_length=64)


class ExotelOutboundRequest(BaseModel):
    # The App ID of the Exotel flow (with the Voicebot step) that outbound calls run when answered.
    app_id: str = Field(min_length=1, max_length=20)


class ExotelSetup(BaseModel):
    """What the person pastes into their Exotel flow; the address is shown only when created."""

    integration: IntegrationRead
    stream_url: str
    steps: list[str]
