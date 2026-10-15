from datetime import date

from pydantic import BaseModel


class AnalyticsOverview(BaseModel):
    customers_count: int
    calls_total: int
    calls_by_status: dict[str, int]
    calls_last_30_days: int
    tickets_total: int
    tickets_by_status: dict[str, int]
    open_tickets: int
    active_campaigns: int


class DailyCount(BaseModel):
    day: date
    count: int


class CallsAnalytics(BaseModel):
    by_status: dict[str, int]
    by_direction: dict[str, int]
    total_duration_seconds: int
    daily_volume: list[DailyCount]


class TicketsAnalytics(BaseModel):
    by_status: dict[str, int]
    by_priority: dict[str, int]
    daily_created: list[DailyCount]


class CampaignsAnalytics(BaseModel):
    by_status: dict[str, int]
    contacts_by_status: dict[str, int]
