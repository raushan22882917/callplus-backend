import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TopupCreate(BaseModel):
    amount_rupees: int = Field(ge=100, le=50_000)


class TopupVerify(BaseModel):
    payment_id: str = Field(min_length=3, max_length=64)
    signature: str = Field(min_length=10, max_length=128)


class BillingProfile(BaseModel):
    billing_name: str = Field(min_length=2, max_length=255)
    gstin: str | None = Field(default=None, max_length=15)
    state_code: str | None = Field(default=None, pattern=r"^\d{2}$")


class TransactionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    amount_paise: int
    balance_after_paise: int
    description: str
    created_at: datetime


class TopupRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    amount_paise: int
    total_paise: int
    invoice_number: str | None
    inbound_rate_paise: int
    outbound_rate_paise: int
    paid_at: datetime | None
    created_at: datetime
