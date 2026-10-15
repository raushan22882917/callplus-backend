import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

_E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def _check_e164(value: str) -> str:
    value = value.strip()
    if not _E164.match(value):
        raise ValueError("phone_number must be E.164, e.g. +919876543210")
    return value


class TransferTargetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    phone_number: str
    is_active: bool = True
    priority: int = Field(default=100, ge=0, le=10_000)

    _validate_phone = field_validator("phone_number")(_check_e164)


class TransferTargetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    phone_number: str | None = None
    is_active: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=10_000)

    @field_validator("phone_number")
    @classmethod
    def _validate_phone(cls, value: str | None) -> str | None:
        return None if value is None else _check_e164(value)


class TransferTargetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    phone_number: str
    is_active: bool
    priority: int
    created_at: datetime
