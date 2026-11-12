import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PackCode = Literal["call", "whatsapp", "email_sms"]


class PackPurchaseCreate(BaseModel):
    """Which packs to buy and how to pay. There is no amount: the server prices it. Choosing all three
    gives the bundle price."""

    items: list[PackCode] = Field(min_length=1, max_length=3)
    provider: Literal["razorpay", "paypal"] = "razorpay"
    billing_name: str | None = Field(default=None, min_length=2, max_length=255)
    gstin: str | None = Field(default=None, max_length=15)
    state_code: str | None = Field(default=None, pattern=r"^\d{2}$")

    @field_validator("items")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class PackVerify(BaseModel):
    payment_id: str = Field(min_length=3, max_length=64)
    signature: str = Field(min_length=10, max_length=128)


class PackPayPalCapture(BaseModel):
    order_id: str = Field(min_length=3, max_length=64)


class PackPurchaseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    provider: str
    items: list[str]
    bundle: bool
    amount_paise: int
    discount_paise: int
    total_paise: int
    invoice_number: str | None
    paid_at: datetime | None
    created_at: datetime
