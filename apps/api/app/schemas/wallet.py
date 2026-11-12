import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TopupCreate(BaseModel):
    amount_rupees: int = Field(ge=100, le=50_000)
    provider: Literal["razorpay", "paypal"] = "razorpay"


class TopupVerify(BaseModel):
    payment_id: str = Field(min_length=3, max_length=64)
    signature: str = Field(min_length=10, max_length=128)


class PayPalCapture(BaseModel):
    """PayPal's return URL puts the order id in `token`. The dashboard sends it here."""

    order_id: str = Field(min_length=3, max_length=64)


class ServiceChoice(BaseModel):
    """Which services this workspace wants. A turned-off service is not sent and is not charged."""

    calls: bool = True
    whatsapp: bool = True
    sms: bool = True
    email: bool = True


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
    provider: str = "razorpay"
    amount_paise: int
    total_paise: int
    charged_cents: int | None = None
    fx_inr_per_usd: int | None = None
    invoice_number: str | None
    inbound_rate_paise: int
    outbound_rate_paise: int
    paid_at: datetime | None
    created_at: datetime
