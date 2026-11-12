import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator


def _check_password_bytes(value: str) -> str:
    # bcrypt only uses the first 72 bytes; longer input is rejected, not truncated.
    if len(value.encode("utf-8")) > 72:
        raise ValueError("password must be at most 72 bytes")
    return value


class SignupRequest(BaseModel):
    company_name: str = Field(min_length=2, max_length=255)
    tenant_slug: str = Field(min_length=2, max_length=100, pattern=r"^[a-z0-9-]+$")
    full_name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)

    _password_bytes = field_validator("password")(_check_password_bytes)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    tenant_slug: str | None = None
    # Required when the account has 2FA enabled (TOTP or a recovery code).
    totp_code: str | None = None


class TenantOption(BaseModel):
    tenant_id: uuid.UUID
    tenant_name: str
    tenant_slug: str
    role_name: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class LoginResponse(BaseModel):
    requires_tenant_selection: bool = False
    # True when the password was right but a 2FA code must be sent too.
    requires_2fa: bool = False
    tenants: list[TenantOption] = []
    tokens: TokenPair | None = None


class RefreshRequest(BaseModel):
    refresh_token: str


class SwitchTenantRequest(BaseModel):
    tenant_slug: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=72)

    _password_bytes = field_validator("new_password")(_check_password_bytes)


class MessageResponse(BaseModel):
    message: str


class SessionRead(BaseModel):
    id: uuid.UUID
    device: str
    ip_address: str | None
    created_at: datetime
    last_seen_at: datetime
    current: bool


class RevokedCount(BaseModel):
    revoked: int
