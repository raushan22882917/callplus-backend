"""The tool registry: the fixed set of actions an AI agent may take, each
backed by a tenant-scoped handler (app/tools/handlers.py). An agent's
ai_agent_tools rows say which of these it may call — the registry itself is
platform code, not tenant-configurable, so a tenant can enable/disable a
tool but never redefine what it does.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.tools import handlers
from app.tools.context import ToolContext

ToolHandler = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def as_llm_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


TOOL_REGISTRY: dict[str, ToolSpec] = {
    "search_knowledge_base": ToolSpec(
        name="search_knowledge_base",
        description="Search the company's knowledge base (FAQs, policies, product docs) "
        "and return the most relevant passages. Use this to answer factual questions "
        "instead of guessing.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look up"},
                "limit": {"type": "integer", "default": 3},
            },
            "required": ["query"],
        },
        handler=handlers.search_knowledge_base,
    ),
    "get_customer": ToolSpec(
        name="get_customer",
        description="Look up the caller's customer record by phone number. "
        "Omit phone to use the customer already tied to this conversation.",
        parameters={
            "type": "object",
            "properties": {"phone": {"type": "string", "description": "E.164 phone number"}},
        },
        handler=handlers.get_customer,
    ),
    "list_recent_tickets": ToolSpec(
        name="list_recent_tickets",
        description="List a customer's most recent support tickets.",
        parameters={
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
            },
        },
        handler=handlers.list_recent_tickets,
    ),
    "create_ticket": ToolSpec(
        name="create_ticket",
        description="Open a new support ticket for the customer.",
        parameters={
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "urgent"],
                    "default": "medium",
                },
                "customer_id": {"type": "string"},
                "caller_name": {
                    "type": "string",
                    "description": "The caller's name, if they told you. Saved on their record.",
                },
                "contact_phone": {
                    "type": "string",
                    "description": "Only if the caller wants to be reached on a different number than the one they're calling from.",
                },
            },
            "required": ["subject"],
        },
        handler=handlers.create_ticket,
    ),
    "add_customer_note": ToolSpec(
        name="add_customer_note",
        description="Record a free-text note on the customer's timeline.",
        parameters={
            "type": "object",
            "properties": {"note": {"type": "string"}, "customer_id": {"type": "string"}},
            "required": ["note"],
        },
        handler=handlers.add_customer_note,
    ),
    "schedule_callback": ToolSpec(
        name="schedule_callback",
        description="Record a request for a human agent to call the customer back "
        "at a requested time. This does not book a calendar slot — it flags the "
        "request for staff to action.",
        parameters={
            "type": "object",
            "properties": {
                "requested_time": {
                    "type": "string",
                    "description": (
                        "The time the customer asked for, in their own words or ISO 8601"
                    ),
                },
                "note": {"type": "string"},
                "customer_id": {"type": "string"},
                "caller_name": {
                    "type": "string",
                    "description": "The caller's name, if they told you. Saved on their record.",
                },
                "contact_phone": {
                    "type": "string",
                    "description": "Only if the caller wants to be called back on a different number than the one they're calling from.",
                },
            },
            "required": ["requested_time"],
        },
        handler=handlers.schedule_callback,
    ),
    "send_whatsapp_message": ToolSpec(
        name="send_whatsapp_message",
        description="Send a WhatsApp text message to a phone number on the business's "
        "connected WhatsApp number.",
        parameters={
            "type": "object",
            "properties": {"phone": {"type": "string"}, "message": {"type": "string"}},
            "required": ["phone", "message"],
        },
        handler=handlers.send_whatsapp_message,
    ),
    "create_customer_payment_link": ToolSpec(
        name="create_customer_payment_link",
        description="Create a Razorpay payment link for the customer to pay an amount, "
        "in INR or USD. Returns a short URL to read out or send to the customer.",
        parameters={
            "type": "object",
            "properties": {
                "amount": {"type": "number"},
                "currency": {"type": "string", "enum": ["INR", "USD"]},
                "description": {"type": "string"},
                "customer_id": {"type": "string"},
            },
            "required": ["amount", "description"],
        },
        handler=handlers.create_customer_payment_link,
    ),
    "transfer_call": ToolSpec(
        name="transfer_call",
        description="Transfer the call to a human agent. Use this when the customer "
        "explicitly asks for a human, or the request is outside what you can help with.",
        parameters={
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
        handler=handlers.transfer_call,
    ),
}


async def execute_tool(name: str, ctx: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    spec = TOOL_REGISTRY.get(name)
    if spec is None:
        return {"error": f"unknown tool: {name}"}
    try:
        return await spec.handler(ctx, **arguments)
    except TypeError as exc:
        return {"error": f"invalid arguments for {name}: {exc}"}


def _register_module_tools() -> None:
    """Tools defined in their own modules (crm, operations); merged here, after
    ToolSpec exists, so those modules can import it without a circular import."""
    from app.tools import crm_tools, operations_tools, platform_tools

    for specs in (
        crm_tools.build_tool_specs(),
        operations_tools.TOOL_SPECS,
        platform_tools.TOOL_SPECS,
    ):
        for name, spec in specs.items():
            TOOL_REGISTRY[name] = spec if isinstance(spec, ToolSpec) else ToolSpec(**spec)


_register_module_tools()
