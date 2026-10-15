import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from packages.database.models import PaymentLinkStatus

# Razorpay Payment Links support other currencies (USD included) only on
# merchant accounts enabled for international payments; on a standard
# Indian account a non-INR request is rejected by Razorpay itself, which
# surfaces to the caller as a 424 via RazorpayAPIError. See
# app/integrations/razorpay_client.py for the request itself.
SupportedCurrency = Literal["INR", "USD"]


class PaymentLinkCreate(BaseModel):
    customer_id: uuid.UUID | None = None
    amount: float = Field(gt=0)
    currency: SupportedCurrency = "INR"
    description: str = Field(min_length=1, max_length=500)


class PaymentLinkRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    customer_id: uuid.UUID | None
    amount_paise: int
    currency: str
    description: str | None
    short_url: str
    status: PaymentLinkStatus
    created_at: datetime
