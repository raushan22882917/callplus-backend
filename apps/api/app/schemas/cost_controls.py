import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class CostControlsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    monthly_budget: float | None
    alert_thresholds: list[int]
    alert_emails: list[str]
    pause_outbound_at_budget: bool
    daily_outbound_call_cap: int | None
    max_concurrent_outbound: int | None


class CostControlsUpdate(BaseModel):
    """PATCH body. A field set to null clears that limit; a field left out is unchanged."""

    monthly_budget: float | None = Field(default=None, ge=0, le=1_000_000_000)
    alert_thresholds: list[int] | None = None
    alert_emails: list[str] | None = None
    pause_outbound_at_budget: bool | None = None
    daily_outbound_call_cap: int | None = Field(default=None, ge=1, le=1_000_000)
    max_concurrent_outbound: int | None = Field(default=None, ge=1, le=10_000)

    @field_validator("alert_thresholds")
    @classmethod
    def _thresholds(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return value
        if len(value) > 6:
            raise ValueError("use at most 6 alert levels")
        if any(not 1 <= v <= 200 for v in value):
            raise ValueError("alert levels are percentages between 1 and 200")
        return sorted(set(value))

    @field_validator("alert_emails")
    @classmethod
    def _emails(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        cleaned = sorted({e.strip().lower() for e in value if e.strip()})
        if len(cleaned) > 5:
            raise ValueError("use at most 5 alert emails")
        bad = [e for e in cleaned if len(e) > 254 or not _EMAIL.match(e)]
        if bad:
            raise ValueError(f"not an email address: {bad[0]}")
        return cleaned


class CostStatusRead(BaseModel):
    period: str
    currency: str
    spend: float
    budget: float | None
    percent_used: float | None
    level: int
    forecast: float
    outbound_paused: bool
    outbound_today: int
    daily_outbound_call_cap: int | None
    outbound_in_flight: int
    max_concurrent_outbound: int | None


class CostAlertRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    period: str
    level: int
    spend: float
    budget: float | None
    message: str
    created_at: datetime
