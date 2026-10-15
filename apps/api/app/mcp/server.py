"""The MCP server this platform exposes to external MCP clients — ChatGPT,
Claude (Desktop, web, or Code), or any other MCP host a tenant connects.

Mirrors the tool registry AI agents use on live calls (app/tools/registry.py)
plus trigger_call (places one), so the same actions ("look up this
customer", "open a ticket", "call this number") are reachable both from a
phone conversation and from a chat with an external LLM. Every tool call is
independently authenticated (see app/mcp/auth.py) and tenant-scoped — there
is no server-side session tied to a human login the way RequestContext is,
so nothing here can assume a tenant_id the way an in-call ToolContext can.

Built once at import time and mounted into the FastAPI app in app/main.py.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from app.core.config import get_settings
from app.mcp.auth import McpAuthError, resolve_tenant
from app.services.outbound_call import OutboundCallError, place_ai_outbound_call
from app.tools import handlers
from app.tools.context import ToolContext
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.session import session_scope

mcp = MCPServer(
    "AutoNxt Call Center",
    instructions=(
        "Tools for a multi-tenant AI call-center platform: look up "
        "customers, manage support tickets, send WhatsApp messages, create "
        "payment links, and place outbound AI phone calls. Every call acts "
        "on behalf of whichever tenant issued the connected API key."
    ),
)


def _transport_security() -> TransportSecuritySettings:
    """The SDK's default Host allowlist only covers 127.0.0.1/localhost —
    meant for a browser tab hitting a local dev server. That's the wrong
    default for us: our caller is ChatGPT/Claude's backend over the public
    internet, so left as-is it would reject every real request with 421
    once this is deployed behind a real hostname. Scope the allowlist to
    this service's own configured public host instead of disabling the
    check — every request is also independently API-key-authenticated
    (app/mcp/auth.py), but there's no reason to drop this layer too."""
    public_host = urlparse(get_settings().public_base_url).netloc
    allowed_hosts = {"127.0.0.1:*", "localhost:*", "[::1]:*"}
    if public_host:
        allowed_hosts.add(public_host)
        allowed_hosts.add(f"{public_host.split(':')[0]}:*")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(allowed_hosts),
    )


# Built once here (not lazily) — MCPServer.session_manager raises until
# streamable_http_app() has run, and app/main.py's lifespan needs that
# property before the first request arrives.
mcp_asgi_app = mcp.streamable_http_app(
    streamable_http_path="/",
    stateless_http=True,
    transport_security=_transport_security(),
)


@asynccontextmanager
async def _tenant_session(ctx: Context) -> AsyncIterator[tuple[AsyncSession, uuid.UUID]]:
    async with session_scope() as db:
        try:
            tenant_id = await resolve_tenant(ctx.headers, db)
        except McpAuthError as exc:
            raise ToolError(str(exc)) from exc
        yield db, tenant_id
        await db.commit()


def _tool_ctx(db: AsyncSession, tenant_id: uuid.UUID) -> ToolContext:
    return ToolContext(
        db=db,
        tenant_id=tenant_id,
        agent_id=None,
        call_id=None,
        conversation_id=None,
        customer_id=None,
    )


@mcp.tool()
async def get_customer(ctx: Context, phone: str | None = None) -> dict:
    """Look up a customer by phone number, in E.164 format (e.g. +14155551234)."""
    async with _tenant_session(ctx) as (db, tenant_id):
        return await handlers.get_customer(_tool_ctx(db, tenant_id), phone=phone)


@mcp.tool()
async def list_recent_tickets(ctx: Context, customer_id: str, limit: int = 5) -> dict:
    """List a customer's most recent support tickets."""
    async with _tenant_session(ctx) as (db, tenant_id):
        return await handlers.list_recent_tickets(
            _tool_ctx(db, tenant_id), customer_id=customer_id, limit=limit
        )


@mcp.tool()
async def create_ticket(
    ctx: Context, subject: str, customer_id: str, priority: str = "medium"
) -> dict:
    """Open a new support ticket for a customer. priority is one of
    low, medium, high, urgent."""
    async with _tenant_session(ctx) as (db, tenant_id):
        return await handlers.create_ticket(
            _tool_ctx(db, tenant_id), subject=subject, priority=priority, customer_id=customer_id
        )


@mcp.tool()
async def add_customer_note(ctx: Context, note: str, customer_id: str) -> dict:
    """Record a free-text note on a customer's timeline."""
    async with _tenant_session(ctx) as (db, tenant_id):
        return await handlers.add_customer_note(
            _tool_ctx(db, tenant_id), note=note, customer_id=customer_id
        )


@mcp.tool()
async def schedule_callback(
    ctx: Context, requested_time: str, customer_id: str, note: str = ""
) -> dict:
    """Record a request for a human agent to call the customer back. Does
    not book a calendar slot — it flags the request for staff to action."""
    async with _tenant_session(ctx) as (db, tenant_id):
        return await handlers.schedule_callback(
            _tool_ctx(db, tenant_id),
            requested_time=requested_time,
            note=note,
            customer_id=customer_id,
        )


@mcp.tool()
async def send_whatsapp_message(ctx: Context, phone: str, message: str) -> dict:
    """Send a WhatsApp text message from the tenant's connected WhatsApp number."""
    async with _tenant_session(ctx) as (db, tenant_id):
        return await handlers.send_whatsapp_message(
            _tool_ctx(db, tenant_id), phone=phone, message=message
        )


@mcp.tool()
async def create_customer_payment_link(
    ctx: Context,
    amount: float,
    description: str,
    currency: str = "INR",
    customer_id: str | None = None,
) -> dict:
    """Create a Razorpay payment link (INR or USD) for a customer. Returns a
    short URL to send to the customer."""
    async with _tenant_session(ctx) as (db, tenant_id):
        return await handlers.create_customer_payment_link(
            _tool_ctx(db, tenant_id),
            amount=amount,
            description=description,
            currency=currency,
            customer_id=customer_id,
        )


@mcp.tool()
async def trigger_call(
    ctx: Context,
    to_number: str,
    ai_agent_id: str | None = None,
    from_number: str | None = None,
) -> dict:
    """Place an outbound AI phone call to a customer, in E.164 format
    (e.g. +14155551234). ai_agent_id and from_number can both be omitted if
    the tenant has exactly one AI agent and phone number configured;
    otherwise specify which agent should handle the call and which of the
    tenant's numbers it should call from."""
    async with _tenant_session(ctx) as (db, tenant_id):
        try:
            call = await place_ai_outbound_call(
                db,
                tenant_id,
                to_number=to_number,
                ai_agent_id=ai_agent_id,
                from_number=from_number,
            )
        except OutboundCallError as exc:
            raise ToolError(str(exc)) from exc
        return {"call_id": str(call.id), "status": call.status.value, "to_number": call.to_number}
