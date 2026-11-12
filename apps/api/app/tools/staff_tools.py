"""Tools only the staff assistant may call. A phone agent and a workflow
cannot reach these: execute_tool refuses them unless ToolContext.staff is set.
Handlers flush and do not commit; the assistant turn commits once."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from app.services import plans
from app.services.plans import PlanLimitError
from app.tools.context import ToolContext
from app.tools.handlers import _normalize_e164
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from packages.database.models import Customer, Ticket
from packages.database.models.agent import AIAgent, AgentStatus
from packages.database.models.blocklist import BlockedCaller
from packages.database.models.campaign import Campaign, CampaignChannel, CampaignStatus
from packages.database.models.crm import CrmDeal, CrmLead, CrmStage, CrmTask, CrmTaskStatus, CrmTaskType
from packages.database.models.knowledge import KnowledgeDocument, KnowledgeDocumentSource
from packages.database.models.operations import Appointment
from packages.database.models.call import Call
from packages.database.models.telephony import PhoneNumber
from packages.database.models.ticket import TicketComment, TicketPriority, TicketStatus


def _limit(value: int | None, default: int = 10) -> int:
    try:
        count = int(value if value is not None else default)
    except (TypeError, ValueError):
        count = default
    return max(1, min(count, 20))


def _uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _need_confirm(confirm: bool) -> dict | None:
    if confirm:
        return None
    return {
        "error": "This removes a record. Ask the person to confirm, then call again with confirm true."
    }


async def _owned(ctx: ToolContext, model, record_id: str):
    parsed = _uuid(record_id)
    if parsed is None:
        return None
    row = await ctx.db.get(model, parsed)
    if row is None or getattr(row, "tenant_id", None) != ctx.tenant_id:
        return None
    return row


def _customer(row: Customer) -> dict:
    return {
        "customer_id": str(row.id),
        "name": row.name,
        "phone": row.phone,
        "email": row.email,
        "tags": list(row.tags or []),
    }


async def search_customers(ctx: ToolContext, *, query: str, limit: int = 10) -> dict:
    needle = (query or "").strip()[:100]
    if not needle:
        return {"error": "Say who to look for"}
    like = f"%{needle}%"
    rows = (
        await ctx.db.execute(
            select(Customer)
            .where(
                Customer.tenant_id == ctx.tenant_id,
                or_(Customer.name.ilike(like), Customer.email.ilike(like), Customer.phone.ilike(like)),
            )
            .order_by(Customer.created_at.desc())
            .limit(_limit(limit))
        )
    ).scalars()
    return {"customers": [_customer(row) for row in rows]}


async def create_customer(
    ctx: ToolContext, *, name: str, phone: str | None = None, email: str | None = None
) -> dict:
    name = (name or "").strip()[:255]
    if not name:
        return {"error": "A name is required"}
    stored_phone = _normalize_e164(phone) if phone and phone.strip() else None
    if stored_phone:
        existing = (
            await ctx.db.execute(
                select(Customer).where(Customer.tenant_id == ctx.tenant_id, Customer.phone == stored_phone)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return {**_customer(existing), "created": False}
    row = Customer(
        tenant_id=ctx.tenant_id,
        name=name,
        phone=stored_phone,
        email=email.strip().lower()[:255] if email and email.strip() else None,
    )
    ctx.db.add(row)
    await ctx.db.flush()
    return {**_customer(row), "created": True}


async def update_customer(
    ctx: ToolContext,
    *,
    customer_id: str,
    name: str | None = None,
    phone: str | None = None,
    email: str | None = None,
) -> dict:
    row = await _owned(ctx, Customer, customer_id)
    if row is None:
        return {"error": "Customer not found"}
    if name and name.strip():
        row.name = name.strip()[:255]
    if phone is not None:
        row.phone = _normalize_e164(phone) if phone.strip() else None
    if email is not None:
        row.email = email.strip().lower()[:255] or None
    await ctx.db.flush()
    return _customer(row)


async def delete_customer(ctx: ToolContext, *, customer_id: str, confirm: bool = False) -> dict:
    refused = _need_confirm(confirm)
    if refused:
        return refused
    row = await _owned(ctx, Customer, customer_id)
    if row is None:
        return {"error": "Customer not found"}
    label = row.name
    try:
        async with ctx.db.begin_nested():
            await ctx.db.delete(row)
            await ctx.db.flush()
    except IntegrityError:
        return {"error": "That customer is still used by other records, so it was not deleted"}
    return {"deleted": True, "name": label}


async def update_ticket(
    ctx: ToolContext,
    *,
    ticket_id: str,
    status: str | None = None,
    priority: str | None = None,
    subject: str | None = None,
) -> dict:
    row = await _owned(ctx, Ticket, ticket_id)
    if row is None:
        return {"error": "Ticket not found"}
    try:
        if status:
            row.status = TicketStatus(status)
        if priority:
            row.priority = TicketPriority(priority)
    except ValueError:
        return {"error": "status must be open, pending, resolved or closed; priority low, medium, high or urgent"}
    if subject and subject.strip():
        row.subject = subject.strip()[:255]
    await ctx.db.flush()
    return {
        "ticket_id": str(row.id),
        "subject": row.subject,
        "status": row.status.value,
        "priority": row.priority.value,
    }


async def add_ticket_comment(ctx: ToolContext, *, ticket_id: str, body: str) -> dict:
    row = await _owned(ctx, Ticket, ticket_id)
    text = (body or "").strip()
    if row is None:
        return {"error": "Ticket not found"}
    if not text:
        return {"error": "The comment is empty"}
    comment = TicketComment(tenant_id=ctx.tenant_id, ticket_id=row.id, body=text[:4000])
    ctx.db.add(comment)
    await ctx.db.flush()
    return {"comment_id": str(comment.id), "ticket_id": str(row.id)}


def _agent(row: AIAgent) -> dict:
    return {
        "agent_id": str(row.id),
        "name": row.name,
        "status": row.status.value,
        "language": row.language,
        "voice": row.voice,
        "greeting": row.greeting,
    }


async def list_agents(ctx: ToolContext, *, limit: int = 20) -> dict:
    rows = (
        await ctx.db.execute(
            select(AIAgent)
            .where(AIAgent.tenant_id == ctx.tenant_id)
            .order_by(AIAgent.created_at.desc())
            .limit(_limit(limit, 20))
        )
    ).scalars()
    return {"agents": [_agent(row) for row in rows]}


async def create_agent(
    ctx: ToolContext, *, name: str, greeting: str | None = None, system_prompt: str | None = None
) -> dict:
    name = (name or "").strip()[:255]
    if not name:
        return {"error": "A name is required"}
    try:
        await plans.ensure_can_create(ctx.db, ctx.tenant_id, "agents")
    except PlanLimitError as exc:
        return {"error": exc.message}
    row = AIAgent(tenant_id=ctx.tenant_id, name=name, status=AgentStatus.DRAFT)
    if greeting and greeting.strip():
        row.greeting = greeting.strip()[:1000]
    if system_prompt and system_prompt.strip():
        row.system_prompt = system_prompt.strip()[:8000]
    ctx.db.add(row)
    await ctx.db.flush()
    return {**_agent(row), "created": True}


async def update_agent(
    ctx: ToolContext,
    *,
    agent_id: str,
    name: str | None = None,
    greeting: str | None = None,
    system_prompt: str | None = None,
    status: str | None = None,
    language: str | None = None,
    voice: str | None = None,
) -> dict:
    row = await _owned(ctx, AIAgent, agent_id)
    if row is None:
        return {"error": "Agent not found"}
    if name and name.strip():
        row.name = name.strip()[:255]
    if greeting is not None:
        row.greeting = greeting.strip()[:1000] or None
    if system_prompt and system_prompt.strip():
        row.system_prompt = system_prompt.strip()[:8000]
    if language and language.strip():
        row.language = language.strip()[:10]
    if voice is not None:
        row.voice = voice.strip()[:100] or None
    if status:
        try:
            row.status = AgentStatus(status)
        except ValueError:
            return {"error": "status must be draft, active or disabled"}
    await ctx.db.flush()
    return _agent(row)


async def delete_agent(ctx: ToolContext, *, agent_id: str, confirm: bool = False) -> dict:
    refused = _need_confirm(confirm)
    if refused:
        return refused
    row = await _owned(ctx, AIAgent, agent_id)
    if row is None:
        return {"error": "Agent not found"}
    label = row.name
    try:
        async with ctx.db.begin_nested():
            await ctx.db.delete(row)
            await ctx.db.flush()
    except IntegrityError:
        return {"error": "That agent is still used. Turn it off instead of deleting it."}
    return {"deleted": True, "name": label}


def _campaign(row: Campaign) -> dict:
    return {
        "campaign_id": str(row.id),
        "name": row.name,
        "channel": row.channel.value,
        "status": row.status.value,
        "goal": row.goal,
    }


async def list_campaigns(ctx: ToolContext, *, limit: int = 20) -> dict:
    rows = (
        await ctx.db.execute(
            select(Campaign)
            .where(Campaign.tenant_id == ctx.tenant_id)
            .order_by(Campaign.created_at.desc())
            .limit(_limit(limit, 20))
        )
    ).scalars()
    return {"campaigns": [_campaign(row) for row in rows]}


async def create_campaign(ctx: ToolContext, *, name: str, channel: str, goal: str | None = None) -> dict:
    name = (name or "").strip()[:255]
    if not name:
        return {"error": "A name is required"}
    try:
        kind = CampaignChannel(channel)
    except ValueError:
        return {"error": "channel must be voice, whatsapp or email"}
    try:
        await plans.ensure_feature(ctx.db, ctx.tenant_id, "campaigns")
    except PlanLimitError as exc:
        return {"error": exc.message}
    row = Campaign(
        tenant_id=ctx.tenant_id,
        name=name,
        channel=kind,
        status=CampaignStatus.DRAFT,
        goal=(goal or "").strip()[:4000] or None,
    )
    ctx.db.add(row)
    await ctx.db.flush()
    return {**_campaign(row), "created": True}


async def update_campaign(
    ctx: ToolContext,
    *,
    campaign_id: str,
    name: str | None = None,
    goal: str | None = None,
    status: str | None = None,
) -> dict:
    row = await _owned(ctx, Campaign, campaign_id)
    if row is None:
        return {"error": "Campaign not found"}
    if name and name.strip():
        row.name = name.strip()[:255]
    if goal is not None:
        row.goal = goal.strip()[:4000] or None
    if status:
        if status == "running":
            return {"error": "Starting a campaign sends messages. Do that from the Campaigns page."}
        try:
            row.status = CampaignStatus(status)
        except ValueError:
            return {"error": "status must be draft, scheduled, paused or completed"}
    await ctx.db.flush()
    return _campaign(row)


async def list_leads(ctx: ToolContext, *, query: str | None = None, limit: int = 10) -> dict:
    statement = select(CrmLead).where(CrmLead.tenant_id == ctx.tenant_id)
    if query and query.strip():
        like = f"%{query.strip()[:100]}%"
        statement = statement.where(or_(CrmLead.name.ilike(like), CrmLead.phone.ilike(like), CrmLead.email.ilike(like)))
    rows = (
        await ctx.db.execute(statement.order_by(CrmLead.created_at.desc()).limit(_limit(limit)))
    ).scalars()
    return {
        "leads": [
            {
                "lead_id": str(row.id),
                "name": row.name,
                "phone": row.phone,
                "email": row.email,
                "stage": row.stage.value,
                "value": float(row.value or 0),
            }
            for row in rows
        ]
    }


async def update_lead(
    ctx: ToolContext,
    *,
    lead_id: str,
    name: str | None = None,
    stage: str | None = None,
    phone: str | None = None,
    email: str | None = None,
) -> dict:
    row = await _owned(ctx, CrmLead, lead_id)
    if row is None:
        return {"error": "Lead not found"}
    if name and name.strip():
        row.name = name.strip()[:255]
    if phone is not None:
        row.phone = phone.strip()[:32] or None
    if email is not None:
        row.email = email.strip().lower()[:255] or None
    if stage:
        try:
            row.stage = CrmStage(stage)
            row.stage_changed_at = datetime.now(UTC)
        except ValueError:
            return {"error": "stage must be one of the CRM stages, such as new_lead, contacted, qualified, won or lost"}
    await ctx.db.flush()
    return {"lead_id": str(row.id), "name": row.name, "stage": row.stage.value}


async def delete_lead(ctx: ToolContext, *, lead_id: str, confirm: bool = False) -> dict:
    refused = _need_confirm(confirm)
    if refused:
        return refused
    row = await _owned(ctx, CrmLead, lead_id)
    if row is None:
        return {"error": "Lead not found"}
    label = row.name
    await ctx.db.delete(row)
    await ctx.db.flush()
    return {"deleted": True, "name": label}


async def list_deals(ctx: ToolContext, *, limit: int = 10) -> dict:
    rows = (
        await ctx.db.execute(
            select(CrmDeal)
            .where(CrmDeal.tenant_id == ctx.tenant_id)
            .order_by(CrmDeal.created_at.desc())
            .limit(_limit(limit))
        )
    ).scalars()
    return {
        "deals": [
            {"deal_id": str(row.id), "title": row.title, "stage": row.stage.value, "value": float(row.value or 0)}
            for row in rows
        ]
    }


async def create_deal(ctx: ToolContext, *, title: str, value: float | None = None, stage: str | None = None) -> dict:
    title = (title or "").strip()[:255]
    if not title:
        return {"error": "A title is required"}
    try:
        chosen = CrmStage(stage) if stage else CrmStage.QUALIFIED
    except ValueError:
        return {"error": "Unknown deal stage"}
    amount = Decimal("0")
    if value is not None:
        try:
            amount = Decimal(str(value))
        except InvalidOperation:
            return {"error": "value must be a number"}
    row = CrmDeal(tenant_id=ctx.tenant_id, title=title, stage=chosen, value=amount)
    ctx.db.add(row)
    await ctx.db.flush()
    return {"deal_id": str(row.id), "title": row.title, "stage": row.stage.value, "created": True}


async def update_deal(
    ctx: ToolContext, *, deal_id: str, title: str | None = None, stage: str | None = None, value: float | None = None
) -> dict:
    row = await _owned(ctx, CrmDeal, deal_id)
    if row is None:
        return {"error": "Deal not found"}
    if title and title.strip():
        row.title = title.strip()[:255]
    if stage:
        try:
            row.stage = CrmStage(stage)
        except ValueError:
            return {"error": "Unknown deal stage"}
    if value is not None:
        try:
            row.value = Decimal(str(value))
        except InvalidOperation:
            return {"error": "value must be a number"}
    await ctx.db.flush()
    return {"deal_id": str(row.id), "title": row.title, "stage": row.stage.value, "value": float(row.value or 0)}


async def list_tasks(ctx: ToolContext, *, limit: int = 10) -> dict:
    rows = (
        await ctx.db.execute(
            select(CrmTask)
            .where(CrmTask.tenant_id == ctx.tenant_id, CrmTask.status == CrmTaskStatus.OPEN)
            .order_by(CrmTask.due_at.asc().nulls_last())
            .limit(_limit(limit))
        )
    ).scalars()
    return {
        "tasks": [
            {"task_id": str(row.id), "title": row.title, "type": row.type.value, "due_at": row.due_at.isoformat() if row.due_at else None}
            for row in rows
        ]
    }


async def create_task(ctx: ToolContext, *, title: str, task_type: str = "todo", lead_id: str | None = None) -> dict:
    title = (title or "").strip()[:255]
    if not title:
        return {"error": "A title is required"}
    try:
        kind = CrmTaskType(task_type)
    except ValueError:
        return {"error": "task_type must be call, whatsapp, email or todo"}
    row = CrmTask(tenant_id=ctx.tenant_id, title=title, type=kind, status=CrmTaskStatus.OPEN)
    lead = _uuid(lead_id)
    if lead is not None:
        owned = await _owned(ctx, CrmLead, str(lead))
        if owned is None:
            return {"error": "Lead not found"}
        row.lead_id = owned.id
    ctx.db.add(row)
    await ctx.db.flush()
    return {"task_id": str(row.id), "title": row.title, "created": True}


async def update_task(ctx: ToolContext, *, task_id: str, status: str) -> dict:
    row = await _owned(ctx, CrmTask, task_id)
    if row is None:
        return {"error": "Task not found"}
    try:
        row.status = CrmTaskStatus(status)
    except ValueError:
        return {"error": "status must be open, done or cancelled"}
    if row.status == CrmTaskStatus.DONE:
        row.completed_at = datetime.now(UTC)
    await ctx.db.flush()
    return {"task_id": str(row.id), "title": row.title, "status": row.status.value}


async def list_knowledge(ctx: ToolContext, *, limit: int = 20) -> dict:
    rows = (
        await ctx.db.execute(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.tenant_id == ctx.tenant_id)
            .order_by(KnowledgeDocument.created_at.desc())
            .limit(_limit(limit, 20))
        )
    ).scalars()
    return {
        "documents": [
            {"document_id": str(row.id), "title": row.title, "status": row.status} for row in rows
        ]
    }


async def create_knowledge(ctx: ToolContext, *, title: str, content: str) -> dict:
    title = (title or "").strip()[:255]
    content = (content or "").strip()
    if not title or not content:
        return {"error": "A title and the text are both required"}
    try:
        await plans.ensure_can_create(ctx.db, ctx.tenant_id, "knowledge_documents")
    except PlanLimitError as exc:
        return {"error": exc.message}
    from app.services import knowledge as knowledge_service

    row = KnowledgeDocument(
        tenant_id=ctx.tenant_id,
        title=title,
        content=content[:50000],
        source=KnowledgeDocumentSource.TEXT,
    )
    ctx.db.add(row)
    await ctx.db.flush()
    await knowledge_service.index_document(ctx.db, row)
    return {"document_id": str(row.id), "title": row.title, "created": True}


async def delete_knowledge(ctx: ToolContext, *, document_id: str, confirm: bool = False) -> dict:
    refused = _need_confirm(confirm)
    if refused:
        return refused
    row = await _owned(ctx, KnowledgeDocument, document_id)
    if row is None:
        return {"error": "Document not found"}
    label = row.title
    await ctx.db.delete(row)
    await ctx.db.flush()
    return {"deleted": True, "title": label}


async def list_blocklist(ctx: ToolContext, *, limit: int = 20) -> dict:
    rows = (
        await ctx.db.execute(
            select(BlockedCaller)
            .where(BlockedCaller.tenant_id == ctx.tenant_id)
            .order_by(BlockedCaller.created_at.desc())
            .limit(_limit(limit, 20))
        )
    ).scalars()
    return {
        "numbers": [{"block_id": str(row.id), "phone": row.e164_number, "reason": row.reason} for row in rows]
    }


async def add_blocklist(ctx: ToolContext, *, phone: str, reason: str | None = None) -> dict:
    if not phone or not phone.strip():
        return {"error": "A phone number is required"}
    number = _normalize_e164(phone)
    existing = (
        await ctx.db.execute(
            select(BlockedCaller).where(
                BlockedCaller.tenant_id == ctx.tenant_id, BlockedCaller.e164_number == number
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {"block_id": str(existing.id), "phone": number, "created": False}
    row = BlockedCaller(tenant_id=ctx.tenant_id, e164_number=number, reason=(reason or "manual").strip()[:255])
    ctx.db.add(row)
    await ctx.db.flush()
    return {"block_id": str(row.id), "phone": number, "created": True}


async def remove_blocklist(ctx: ToolContext, *, phone: str, confirm: bool = False) -> dict:
    refused = _need_confirm(confirm)
    if refused:
        return refused
    number = _normalize_e164(phone) if phone else ""
    row = (
        await ctx.db.execute(
            select(BlockedCaller).where(
                BlockedCaller.tenant_id == ctx.tenant_id, BlockedCaller.e164_number == number
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return {"error": "That number is not blocked"}
    await ctx.db.delete(row)
    await ctx.db.flush()
    return {"deleted": True, "phone": number}


async def list_phone_numbers(ctx: ToolContext) -> dict:
    rows = (
        await ctx.db.execute(select(PhoneNumber).where(PhoneNumber.tenant_id == ctx.tenant_id))
    ).scalars()
    return {
        "numbers": [
            {"phone_number_id": str(row.id), "phone": row.e164_number, "agent_id": str(row.ai_agent_id) if row.ai_agent_id else None}
            for row in rows
        ]
    }


async def assign_phone_number(ctx: ToolContext, *, phone_number_id: str, agent_id: str | None = None) -> dict:
    row = await _owned(ctx, PhoneNumber, phone_number_id)
    if row is None:
        return {"error": "Phone number not found"}
    if agent_id:
        agent = await _owned(ctx, AIAgent, agent_id)
        if agent is None:
            return {"error": "Agent not found"}
        row.ai_agent_id = agent.id
    else:
        row.ai_agent_id = None
    await ctx.db.flush()
    return {"phone": row.e164_number, "agent_id": str(row.ai_agent_id) if row.ai_agent_id else None}


async def list_recent_calls(ctx: ToolContext, *, limit: int = 10) -> dict:
    rows = (
        await ctx.db.execute(
            select(Call)
            .where(Call.tenant_id == ctx.tenant_id)
            .order_by(Call.created_at.desc())
            .limit(_limit(limit))
        )
    ).scalars()
    return {
        "calls": [
            {
                "call_id": str(row.id),
                "direction": row.direction.value if hasattr(row.direction, "value") else row.direction,
                "status": row.status.value if hasattr(row.status, "value") else row.status,
                "from": row.from_number,
                "to": row.to_number,
                "duration_seconds": row.duration_seconds,
            }
            for row in rows
        ]
    }


async def list_appointments(ctx: ToolContext, *, limit: int = 10) -> dict:
    now = datetime.now(UTC)
    rows = (
        await ctx.db.execute(
            select(Appointment)
            .where(Appointment.tenant_id == ctx.tenant_id, Appointment.start_at >= now)
            .order_by(Appointment.start_at.asc())
            .limit(_limit(limit))
        )
    ).scalars()
    return {
        "appointments": [
            {
                "appointment_id": str(row.id),
                "start_at": row.start_at.isoformat(),
                "status": row.status.value if hasattr(row.status, "value") else str(row.status),
                "customer_id": str(row.customer_id) if row.customer_id else None,
            }
            for row in rows
        ]
    }


def _spec(name: str, description: str, parameters: dict, handler) -> dict:
    return {"name": name, "description": description, "parameters": parameters, "handler": handler}


_OBJ = "object"

TOOL_SPECS: dict[str, dict] = {
    "search_customers": _spec(
        "search_customers",
        "Find customers by name, phone or email.",
        {"type": _OBJ, "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]},
        search_customers,
    ),
    "create_customer": _spec(
        "create_customer",
        "Add a customer. Returns the existing customer when that phone is already saved.",
        {"type": _OBJ, "properties": {"name": {"type": "string"}, "phone": {"type": "string"}, "email": {"type": "string"}}, "required": ["name"]},
        create_customer,
    ),
    "update_customer": _spec(
        "update_customer",
        "Change a customer's name, phone or email.",
        {"type": _OBJ, "properties": {"customer_id": {"type": "string"}, "name": {"type": "string"}, "phone": {"type": "string"}, "email": {"type": "string"}}, "required": ["customer_id"]},
        update_customer,
    ),
    "delete_customer": _spec(
        "delete_customer",
        "Delete a customer. Only after the staff member has clearly asked to delete them. Pass confirm true.",
        {"type": _OBJ, "properties": {"customer_id": {"type": "string"}, "confirm": {"type": "boolean"}}, "required": ["customer_id", "confirm"]},
        delete_customer,
    ),
    "update_ticket": _spec(
        "update_ticket",
        "Change a ticket's status, priority or subject.",
        {"type": _OBJ, "properties": {"ticket_id": {"type": "string"}, "status": {"type": "string"}, "priority": {"type": "string"}, "subject": {"type": "string"}}, "required": ["ticket_id"]},
        update_ticket,
    ),
    "add_ticket_comment": _spec(
        "add_ticket_comment",
        "Add a comment on a ticket.",
        {"type": _OBJ, "properties": {"ticket_id": {"type": "string"}, "body": {"type": "string"}}, "required": ["ticket_id", "body"]},
        add_ticket_comment,
    ),
    "list_agents": _spec("list_agents", "List this workspace's voice agents.", {"type": _OBJ, "properties": {"limit": {"type": "integer"}}}, list_agents),
    "create_agent": _spec(
        "create_agent",
        "Create a draft voice agent.",
        {"type": _OBJ, "properties": {"name": {"type": "string"}, "greeting": {"type": "string"}, "system_prompt": {"type": "string"}}, "required": ["name"]},
        create_agent,
    ),
    "update_agent": _spec(
        "update_agent",
        "Change an agent's name, greeting, instructions, language, voice or status (draft, active, disabled).",
        {"type": _OBJ, "properties": {"agent_id": {"type": "string"}, "name": {"type": "string"}, "greeting": {"type": "string"}, "system_prompt": {"type": "string"}, "status": {"type": "string"}, "language": {"type": "string"}, "voice": {"type": "string"}}, "required": ["agent_id"]},
        update_agent,
    ),
    "delete_agent": _spec(
        "delete_agent",
        "Delete an agent. Only after the staff member asked to delete it. Pass confirm true.",
        {"type": _OBJ, "properties": {"agent_id": {"type": "string"}, "confirm": {"type": "boolean"}}, "required": ["agent_id", "confirm"]},
        delete_agent,
    ),
    "list_campaigns": _spec("list_campaigns", "List campaigns.", {"type": _OBJ, "properties": {"limit": {"type": "integer"}}}, list_campaigns),
    "create_campaign": _spec(
        "create_campaign",
        "Create a draft campaign. It does not start sending.",
        {"type": _OBJ, "properties": {"name": {"type": "string"}, "channel": {"type": "string", "enum": ["voice", "whatsapp", "email"]}, "goal": {"type": "string"}}, "required": ["name", "channel"]},
        create_campaign,
    ),
    "update_campaign": _spec(
        "update_campaign",
        "Rename a campaign, change its goal, or set status to draft, scheduled, paused or completed. Cannot start it.",
        {"type": _OBJ, "properties": {"campaign_id": {"type": "string"}, "name": {"type": "string"}, "goal": {"type": "string"}, "status": {"type": "string"}}, "required": ["campaign_id"]},
        update_campaign,
    ),
    "list_leads": _spec(
        "list_leads",
        "List CRM leads. Pass query to search by name, phone or email.",
        {"type": _OBJ, "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}},
        list_leads,
    ),
    "update_lead": _spec(
        "update_lead",
        "Change a lead's name, phone, email or stage.",
        {"type": _OBJ, "properties": {"lead_id": {"type": "string"}, "name": {"type": "string"}, "stage": {"type": "string"}, "phone": {"type": "string"}, "email": {"type": "string"}}, "required": ["lead_id"]},
        update_lead,
    ),
    "delete_lead": _spec(
        "delete_lead",
        "Delete a lead. Only after the staff member asked. Pass confirm true.",
        {"type": _OBJ, "properties": {"lead_id": {"type": "string"}, "confirm": {"type": "boolean"}}, "required": ["lead_id", "confirm"]},
        delete_lead,
    ),
    "list_deals": _spec("list_deals", "List CRM deals.", {"type": _OBJ, "properties": {"limit": {"type": "integer"}}}, list_deals),
    "create_deal": _spec(
        "create_deal",
        "Create a CRM deal.",
        {"type": _OBJ, "properties": {"title": {"type": "string"}, "value": {"type": "number"}, "stage": {"type": "string"}}, "required": ["title"]},
        create_deal,
    ),
    "update_deal": _spec(
        "update_deal",
        "Change a deal's title, stage or value.",
        {"type": _OBJ, "properties": {"deal_id": {"type": "string"}, "title": {"type": "string"}, "stage": {"type": "string"}, "value": {"type": "number"}}, "required": ["deal_id"]},
        update_deal,
    ),
    "list_tasks": _spec("list_tasks", "List open CRM tasks.", {"type": _OBJ, "properties": {"limit": {"type": "integer"}}}, list_tasks),
    "create_task": _spec(
        "create_task",
        "Create a CRM task.",
        {"type": _OBJ, "properties": {"title": {"type": "string"}, "task_type": {"type": "string"}, "lead_id": {"type": "string"}}, "required": ["title"]},
        create_task,
    ),
    "update_task": _spec(
        "update_task",
        "Mark a task open, done or cancelled.",
        {"type": _OBJ, "properties": {"task_id": {"type": "string"}, "status": {"type": "string"}}, "required": ["task_id", "status"]},
        update_task,
    ),
    "list_knowledge": _spec("list_knowledge", "List knowledge documents.", {"type": _OBJ, "properties": {"limit": {"type": "integer"}}}, list_knowledge),
    "create_knowledge": _spec(
        "create_knowledge",
        "Add a text document the agents can search.",
        {"type": _OBJ, "properties": {"title": {"type": "string"}, "content": {"type": "string"}}, "required": ["title", "content"]},
        create_knowledge,
    ),
    "delete_knowledge": _spec(
        "delete_knowledge",
        "Delete a knowledge document. Only after the staff member asked. Pass confirm true.",
        {"type": _OBJ, "properties": {"document_id": {"type": "string"}, "confirm": {"type": "boolean"}}, "required": ["document_id", "confirm"]},
        delete_knowledge,
    ),
    "list_blocklist": _spec("list_blocklist", "List blocked phone numbers.", {"type": _OBJ, "properties": {"limit": {"type": "integer"}}}, list_blocklist),
    "add_blocklist": _spec(
        "add_blocklist",
        "Block a phone number so agents will not answer it.",
        {"type": _OBJ, "properties": {"phone": {"type": "string"}, "reason": {"type": "string"}}, "required": ["phone"]},
        add_blocklist,
    ),
    "remove_blocklist": _spec(
        "remove_blocklist",
        "Unblock a phone number. Only after the staff member asked. Pass confirm true.",
        {"type": _OBJ, "properties": {"phone": {"type": "string"}, "confirm": {"type": "boolean"}}, "required": ["phone", "confirm"]},
        remove_blocklist,
    ),
    "list_phone_numbers": _spec("list_phone_numbers", "List this workspace's phone numbers and which agent answers each.", {"type": _OBJ, "properties": {}}, list_phone_numbers),
    "assign_phone_number": _spec(
        "assign_phone_number",
        "Choose which agent answers a phone number. Omit agent_id to clear it.",
        {"type": _OBJ, "properties": {"phone_number_id": {"type": "string"}, "agent_id": {"type": "string"}}, "required": ["phone_number_id"]},
        assign_phone_number,
    ),
    "list_recent_calls": _spec("list_recent_calls", "List recent calls.", {"type": _OBJ, "properties": {"limit": {"type": "integer"}}}, list_recent_calls),
    "list_appointments": _spec("list_appointments", "List upcoming appointments.", {"type": _OBJ, "properties": {"limit": {"type": "integer"}}}, list_appointments),
}
