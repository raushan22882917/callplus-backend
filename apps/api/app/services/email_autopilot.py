"""Weekly email autopilot: the AI drafts a campaign from the owner's goal and picks the send time.

Two modes. "review" (the default) leaves a draft for the owner to approve with the usual Launch button. "send"
also schedules it, but only when the check scores 70 or more; a weaker draft stays a draft. Either way every
send still goes through the same gates as any campaign: opt-outs and the suppression list, consent, the postal
address in the footer, and the plan's email allowance. The AI writes and times; it never decides who may be mailed.
"""

import logging
import uuid
from datetime import UTC, datetime

from app.core.config import get_settings
from app.integrations.credentials import get_email_sender, get_llm_key
from app.integrations.sarvam import SarvamAPIError
from app.services import email_ai, email_checker, email_compliance, email_insights, send_time
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Campaign,
    CampaignChannel,
    CampaignContact,
    CampaignStatus,
    Customer,
    Tenant,
)

logger = logging.getLogger(__name__)

MODES = ("review", "send")
SEND_MIN_SCORE = 70


def week_key(now: datetime, tz_name: str | None) -> str:
    local = now.astimezone(send_time.zone(tz_name))
    year, week, _ = local.isocalendar()
    return f"{year}-W{week:02d}"


def config(tenant: Tenant) -> dict:
    raw = tenant.email_autopilot or {}
    return {
        "enabled": bool(raw.get("enabled")),
        "goal": str(raw.get("goal") or ""),
        "mode": raw.get("mode") if raw.get("mode") in MODES else "review",
        "day": raw.get("day") if isinstance(raw.get("day"), int) and 0 <= raw["day"] <= 6 else 0,
        "last_week": raw.get("last_week"),
        "last_campaign_id": raw.get("last_campaign_id"),
        "last_run_note": raw.get("last_run_note"),
    }


def save(tenant: Tenant, *, enabled: bool, goal: str, mode: str, day: int) -> None:
    kept = {k: v for k, v in (tenant.email_autopilot or {}).items() if k in ("last_week", "last_campaign_id", "last_run_note")}
    tenant.email_autopilot = {**kept, "enabled": enabled, "goal": goal.strip()[:1000], "mode": mode if mode in MODES else "review", "day": day}


async def not_ready_reason(db: AsyncSession, tenant_id: uuid.UUID) -> str | None:
    """Why this workspace cannot send marketing email yet, or None."""
    from app.services.mailbox import connected_mailbox

    settings = get_settings()
    if (
        await connected_mailbox(db, tenant_id) is None
        and await get_email_sender(db, tenant_id) is None
        and not settings.email_api_key
        and not settings.smtp_host
    ):
        return "Connect a mailbox or an email sending key first."
    if await email_compliance.sender_identity(db, tenant_id) is None:
        return "Add your business name and postal address in Settings > Privacy first."
    return None


async def _recent_results(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    from app.services.email_studio import email_overview

    overview = await email_overview(db, tenant_id)
    lines = []
    for item in overview["campaigns"][:5]:
        if item["sent"]:
            lines.append(f'- "{item["name"]}": {item["sent"]} sent, {round(100 * item["opened"] / item["sent"])}% opened, {item["clicked"]} clicked')
    return "\n".join(lines)


async def run_for_tenant(db: AsyncSession, tenant: Tenant, now: datetime | None = None, *, force: bool = False) -> dict:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    cfg = config(tenant)
    tz_name = tenant.timezone or "UTC"
    key = week_key(now, tz_name)
    if not force:
        if not cfg["enabled"] or not cfg["goal"].strip():
            return {"status": "skipped", "reason": "Autopilot is off."}
        local = now.astimezone(send_time.zone(tz_name))
        if cfg["last_week"] == key or local.weekday() != cfg["day"] or local.hour < 8:
            return {"status": "skipped", "reason": "Not due yet."}
    if not cfg["goal"].strip():
        return {"status": "skipped", "reason": "Tell the autopilot what to send about first."}

    async def finish(result: dict, *, mark: bool = True) -> dict:
        state = dict(tenant.email_autopilot or {})
        state["last_run_note"] = result.get("reason") or result["status"]
        if mark:
            state["last_week"] = key
        if result.get("campaign_id"):
            state["last_campaign_id"] = result["campaign_id"]
        tenant.email_autopilot = state
        await db.commit()
        return result

    problem = await not_ready_reason(db, tenant.id)
    if problem:
        return await finish({"status": "skipped", "reason": problem}, mark=False)

    customers = list(
        (
            await db.execute(
                select(Customer).where(Customer.tenant_id == tenant.id, Customer.email.is_not(None), Customer.email != "").limit(10000)
            )
        ).scalars().all()
    )
    customers = await email_compliance.filter_audience(db, tenant.id, customers)
    if not customers:
        return await finish({"status": "skipped", "reason": "No one can be emailed yet."}, mark=False)

    history = await _recent_results(db, tenant.id)
    instruction = cfg["goal"] + (f"\n\nHow recent emails did (write something fresh, learn from what worked):\n{history}" if history else "")
    try:
        drafted = await email_ai.draft_email(instruction=instruction, current_subject="", current_body="", api_key=await get_llm_key(db, tenant.id))
    except (SarvamAPIError, ValueError) as exc:
        logger.warning("email autopilot could not draft for %s: %s", tenant.id, exc)
        return await finish({"status": "skipped", "reason": "The AI helper could not write this week's email. It will try again."}, mark=False)

    plan = await email_insights.send_time_plan(db, tenant.id, now, count=1)
    slot = plan["slots"][0]["at"] if plan["slots"] else None
    verdict = email_checker.check_email(drafted["subject"], drafted["body"])
    campaign = Campaign(
        tenant_id=tenant.id,
        name=f"Autopilot {now.astimezone(send_time.zone(tz_name)):%d %b}",
        channel=CampaignChannel.EMAIL,
        status=CampaignStatus.DRAFT,
        email_subject=drafted["subject"],
        email_body=drafted["body"],
        scheduled_at=datetime.fromisoformat(slot) if slot else None,
        goal=cfg["goal"][:1000],
    )
    db.add(campaign)
    await db.flush()
    db.add_all([CampaignContact(tenant_id=tenant.id, campaign_id=campaign.id, customer_id=c.id) for c in customers])
    sent = cfg["mode"] == "send" and verdict["score"] >= SEND_MIN_SCORE and slot is not None
    if sent:
        campaign.status = CampaignStatus.SCHEDULED
    reason = (
        f"Scheduled for {plan['slots'][0]['local']} ({verdict['score']}/100 check)."
        if sent
        else f"Draft ready for your approval ({verdict['score']}/100 check)."
    )
    return await finish({"status": "scheduled" if sent else "drafted", "campaign_id": str(campaign.id), "reason": reason, "score": verdict["score"]})


async def run_due(db_factory, now: datetime | None = None) -> int:
    """Called by the scheduler: every workspace whose autopilot is due this week. Returns how many drafted."""
    async with db_factory() as db:
        rows = list((await db.execute(select(Tenant.id).where(Tenant.email_autopilot["enabled"].astext == "true"))).scalars().all())
    made = 0
    for tenant_id in rows:
        async with db_factory() as db:
            tenant = await db.get(Tenant, tenant_id)
            if tenant is None:
                continue
            try:
                result = await run_for_tenant(db, tenant, now)
            except Exception:
                logger.exception("email autopilot failed for %s", tenant_id)
                continue
            made += 1 if result["status"] in ("drafted", "scheduled") else 0
    return made
