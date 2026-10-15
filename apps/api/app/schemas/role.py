import uuid

from pydantic import BaseModel, Field


class RoleCreate(BaseModel):
    name: str = Field(min_length=2, max_length=50)
    permission_codes: list[str] = Field(default_factory=list)


class RoleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=50)
    permission_codes: list[str] | None = None


class RoleRead(BaseModel):
    id: uuid.UUID
    name: str
    is_system: bool
    permission_codes: list[str]


class PermissionRead(BaseModel):
    code: str
    description: str | None
