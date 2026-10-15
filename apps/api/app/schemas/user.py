import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from packages.database.models import MembershipStatus


class MeResponse(BaseModel):
    user_id: uuid.UUID
    email: EmailStr
    full_name: str
    tenant_id: uuid.UUID
    role_name: str
    permission_codes: list[str]
    is_platform_admin: bool = False


class TeamMemberRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    membership_id: uuid.UUID
    user_id: uuid.UUID
    email: EmailStr
    full_name: str
    role_name: str
    status: MembershipStatus
    created_at: datetime


class InviteUserRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=255)
    role_name: str


class UpdateTeamMemberRequest(BaseModel):
    role_name: str | None = None
    status: MembershipStatus | None = None


class InviteUserResponse(BaseModel):
    membership_id: uuid.UUID
    email: EmailStr
    temporary_password: str | None = Field(
        default=None,
        description="Only returned when a brand-new user account was created "
        "and the invite email could not be sent (SMTP unconfigured or failed); "
        "relay it out-of-band. Existing users are simply added to the tenant.",
    )
    email_sent: bool = False


class PlatformAdminRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str
    is_platform_admin: bool


class GrantPlatformAdminRequest(BaseModel):
    email: EmailStr
