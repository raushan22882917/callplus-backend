import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

PHONE_HELP = "Enter the phone number with its country code, like +919876543210."


def clean_phone(value: str | None) -> str | None:
    """A phone number a call or WhatsApp message can actually reach, in one format (+91...).

    Indian numbers typed the usual ways (9876543210, 09876543210, 91 98765 43210) get +91; anything
    else must carry its own country code. A number that can never be dialled is refused here, in plain
    words, instead of being saved and failing later.
    """
    if value is None or not value.strip():
        return None
    text = value.strip()
    digits = re.sub(r"\D", "", text)
    if text.startswith("+"):
        number = "+" + digits
    elif text.startswith("00"):
        number = "+" + digits[2:]
    elif len(digits) == 10 and digits[0] in "6789":
        number = "+91" + digits
    elif len(digits) == 11 and digits[0] == "0" and digits[1] in "6789":
        number = "+91" + digits[1:]
    elif len(digits) == 12 and digits.startswith("91"):
        number = "+" + digits
    else:
        raise ValueError(PHONE_HELP)
    if not re.fullmatch(r"\+[1-9]\d{7,14}", number):
        raise ValueError(PHONE_HELP)
    return number


class CustomerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=255)
    tags: list[str] = Field(default_factory=list)

    _phone = field_validator("phone")(clean_phone)


class CustomerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=255)
    tags: list[str] | None = None

    _phone = field_validator("phone")(clean_phone)


class CustomerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    phone: str | None
    email: str | None
    tags: list[str]
    created_at: datetime
    updated_at: datetime
