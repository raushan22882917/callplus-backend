"""AI-agent tools for the built-in CRM. Same shape as app/tools/registry.py
entries; the lead merges TOOL_SPECS into TOOL_REGISTRY."""

import uuid
from datetime import UTC, datetime, timedelta

from app.services import crm as crm_service
from app.tools.context import ToolContext
from sqlalchemy import select

from packages.database.models.crm import CrmLead, CrmNote, CrmTask, CrmTaskType
from packages.database.models.customer import Customer


async def _find_lead(ctx: ToolContext, *, phone: str | None, email: str | None) -> CrmLead | None:
    if ctx.customer_id:
        res = await ctx.db.execute(
            select(CrmLead)
            .where(CrmLead.tenant_id == ctx.tenant_id, CrmLead.customer_id == ctx.customer_id)
            .order_by(CrmLead.created_at.desc())
        )
        if lead := res.scalars().first():
            return lead
    if phone:
        res = await ctx.db.execute(
            select(CrmLead).where(
                CrmLead.tenant_id == ctx.tenant_id,
                CrmLead.phone.in_(list({phone, crm_service.normalize_phone(phone)})),
            )
        )
        if lead := res.scalars().first():
            return lead
    if email:
        res = await ctx.db.execute(
            select(CrmLead).where(
                CrmLead.tenant_id == ctx.tenant_id, CrmLead.email.ilike(email.strip())
            )
        )
        return res.scalars().first()
    return None


async def create_lead(
    ctx: ToolContext,
    *,
    name: str | None = None,
    phone: str | None = None,
    email: str | None = None,
    interest: str | None = None,
    value: float | None = None,
) -> dict:
    customer = await ctx.db.get(Customer, ctx.customer_id) if ctx.customer_id else None
    if customer is not None and customer.tenant_id != ctx.tenant_id:
        customer = None
    name = (name or (customer.name if customer else None) or "").strip()
    phone = (phone or (customer.phone if customer else None) or "").strip() or None
    email = (email or (customer.email if customer else None) or "").strip() or None
    if not name or name.lower() in {"unknown", "caller"}:
        name = phone or email or ""
    if not name:
        return {"error": "need at least a name, phone or email to create a lead"}

    existing = await _find_lead(ctx, phone=phone, email=email)
    if existing is not None:
        if interest:
            ctx.db.add(
                CrmNote(
                    tenant_id=ctx.tenant_id,
                    lead_id=existing.id,
                    kind="note",
                    body=f"Caller expressed interest: {interest}",
                )
            )
            await ctx.db.flush()
            await crm_service.rescore_lead(ctx.db, existing)
        return {"lead_id": str(existing.id), "created": False, "stage": existing.stage.value}

    lead = CrmLead(
        tenant_id=ctx.tenant_id,
        name=name[:255],
        phone=phone,
        email=email,
        customer_id=customer.id if customer else None,
        source_call_id=ctx.call_id,
        source="ai_call",
        value=max(0, value or 0),
    )
    ctx.db.add(lead)
    await ctx.db.flush()
    if interest:
        ctx.db.add(
            CrmNote(
                tenant_id=ctx.tenant_id,
                lead_id=lead.id,
                kind="note",
                body=f"Caller expressed interest: {interest}",
            )
        )
        await ctx.db.flush()
    await crm_service.rescore_lead(ctx.db, lead)
    await crm_service.push_lead_to_connected_crms(ctx.db, lead)  # best effort
    return {"lead_id": str(lead.id), "created": True, "stage": lead.stage.value, "score": lead.score}


async def schedule_follow_up(
    ctx: ToolContext,
    *,
    title: str,
    due_in_hours: float | None = None,
    due_at: str | None = None,
    task_type: str = "call",
    notes: str | None = None,
    lead_id: str | None = None,
) -> dict:
    try:
        kind = CrmTaskType(task_type)
    except ValueError:
        return {"error": f"task_type must be one of {[t.value for t in CrmTaskType]}"}

    due: datetime | None = None
    if due_at:
        try:
            due = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
        except ValueError:
            return {"error": "due_at must be ISO 8601, e.g. 2026-10-05T10:00:00+05:30"}
        if due.tzinfo is None:
            due = due.replace(tzinfo=UTC)
    elif due_in_hours is not None:
        due = datetime.now(UTC) + timedelta(hours=float(due_in_hours))
    else:
        due = datetime.now(UTC) + timedelta(hours=24)

    lead: CrmLead | None = None
    if lead_id:
        try:
            lead = await ctx.db.get(CrmLead, uuid.UUID(lead_id))
        except ValueError:
            return {"error": "invalid lead_id"}
        if lead is None or lead.tenant_id != ctx.tenant_id:
            return {"error": "lead not found"}
    else:
        lead = await _find_lead(ctx, phone=None, email=None)

    customer_id = ctx.customer_id or (lead.customer_id if lead else None)
    if customer_id is None and lead is None:
        return {"error": "no lead or customer on this conversation to attach the follow-up to"}

    task = CrmTask(
        tenant_id=ctx.tenant_id,
        title=title[:255],
        description=notes,
        type=kind,
        due_at=due,
        lead_id=lead.id if lead else None,
        customer_id=customer_id,
        created_by_ai=True,
    )
    ctx.db.add(task)
    await ctx.db.flush()
    return {"task_id": str(task.id), "due_at": due.isoformat(), "lead_id": str(lead.id) if lead else None}


def build_tool_specs() -> dict:
    # Imported lazily: registry.py will import this module to merge the specs.
    from app.tools.registry import ToolSpec

    return {
        "create_lead": ToolSpec(
            name="create_lead",
            description="Create a sales lead in the CRM for a caller who wants to buy or is "
            "interested in the product. Reuses the caller's existing lead if there is one. "
            "Omit name/phone/email to use the caller already on this conversation.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "phone": {"type": "string", "description": "E.164 phone number"},
                    "email": {"type": "string"},
                    "interest": {
                        "type": "string",
                        "description": "What the caller wants to buy / is interested in",
                    },
                    "value": {"type": "number", "description": "Estimated deal value, if stated"},
                },
            },
            handler=create_lead,
        ),
        "schedule_follow_up": ToolSpec(
            name="schedule_follow_up",
            description="Create a follow-up task for the sales team (call, whatsapp, email or "
            "todo) linked to the caller's lead. Use when the caller asks to be contacted later.",
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "due_in_hours": {"type": "number", "description": "Hours from now"},
                    "due_at": {"type": "string", "description": "ISO 8601 time; overrides due_in_hours"},
                    "task_type": {"type": "string", "enum": ["call", "whatsapp", "email", "todo"]},
                    "notes": {"type": "string"},
                    "lead_id": {"type": "string"},
                },
                "required": ["title"],
            },
            handler=schedule_follow_up,
        ),
    }


def __getattr__(name: str):
    if name == "TOOL_SPECS":
        return build_tool_specs()
    raise AttributeError(name)
