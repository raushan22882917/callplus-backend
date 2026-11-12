from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class OAuthProviderRead(BaseModel):
    provider: str
    label: str
    category: str
    description: str
    configured: bool
    connected: bool
    status: str | None = None
    account_label: str | None = None
    account_email: str | None = None
    scopes: list[str] = []
    expires_at: datetime | None = None
    # True when the access token renews itself (refresh token or non-expiring).
    auto_refresh: bool = True
    needs_reconnect: bool = False
    last_error: str | None = None
    connected_at: datetime | None = None
    features: list[str] = []
    options: list[str] = []
    requires_shop: bool = False
    resources_kind: str | None = None
    selected: dict[str, Any] | None = None
    redirect_uri: str
    supports_login: bool = False
    # An API token can be pasted instead of signing in (HubSpot, Shopify, Slack).
    token_supported: bool = False
    token_label: str | None = None
    token_steps: str | None = None
    token_url: str | None = None
    token_placeholder: str | None = None
    # True when the saved connection was made with a pasted token, not a sign-in.
    via_token: bool = False


class TokenConnectRequest(BaseModel):
    token: str = Field(min_length=8, max_length=600)
    shop: str | None = Field(default=None, max_length=255)


class AuthorizeRequest(BaseModel):
    return_url: str | None = Field(default=None, max_length=500)
    options: list[str] = []
    shop: str | None = Field(default=None, max_length=255)


class AuthorizeResponse(BaseModel):
    authorization_url: str
    expires_in: int


class ResourceItem(BaseModel):
    id: str
    name: str
    primary: bool = False
    detail: str | None = None


class ResourcesResponse(BaseModel):
    kind: str
    items: list[ResourceItem]
    selected: dict[str, Any] | None = None


class SelectRequest(BaseModel):
    resource_id: str | None = Field(default=None, max_length=255)
    block_busy: bool | None = None
    events: list[str] | None = None


class WhatsAppSignupRequest(BaseModel):
    code: str = Field(min_length=1, max_length=2000)
    phone_number_id: str = Field(min_length=1, max_length=64)
    waba_id: str = Field(min_length=1, max_length=64)
    pin: str | None = Field(default=None, pattern=r"^\d{6}$")


class WhatsAppSignupResponse(BaseModel):
    id: str
    phone_number_id: str
    waba_id: str
    display_phone_number: str
    registration_pin: str | None = None


class CrmPushRequest(BaseModel):
    provider: str | None = None


class CrmPushResult(BaseModel):
    provider: str
    ok: bool
    detail: str
