import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from packages.database.models import BillingInterval, InvoiceStatus, SubscriptionStatus


class PlanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    price_paise: int
    currency: str
    billing_interval: BillingInterval


class PlanAdminRead(PlanRead):
    """Same as PlanRead but for the platform-admin catalog view, which also
    needs to see provider_plan_id (to tell which plans aren't wired to a
    real Razorpay Plan yet) and is_active (to see disabled plans)."""

    provider_plan_id: str | None
    is_active: bool


class PlanCreate(BaseModel):
    code: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=100)
    price_paise: int = Field(gt=0)
    currency: Literal["INR", "USD"] = "INR"
    billing_interval: BillingInterval = BillingInterval.MONTH
    # Must be a real Plan already created on Razorpay's side (dashboard or
    # Plans API) — see packages/database/models/billing.py. Left unset, the
    # plan is visible but subscribing to it fails with a 424 until it's set.
    provider_plan_id: str | None = None


class PlanUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    price_paise: int | None = Field(default=None, gt=0)
    currency: Literal["INR", "USD"] | None = None
    billing_interval: BillingInterval | None = None
    provider_plan_id: str | None = None
    is_active: bool | None = None


class SubscriptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    plan_id: uuid.UUID
    provider_subscription_id: str
    status: SubscriptionStatus
    current_period_end: datetime | None
    cancel_at_period_end: bool
    created_at: datetime


class CreateSubscriptionRequest(BaseModel):
    plan_code: str


class CancelSubscriptionRequest(BaseModel):
    at_period_end: bool = True


class InvoiceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    subscription_id: uuid.UUID | None
    amount_paise: int
    currency: str
    status: InvoiceStatus
    issued_at: datetime
    paid_at: datetime | None
