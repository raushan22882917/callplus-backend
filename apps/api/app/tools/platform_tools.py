"""AI-agent tools backed by the connector hub. The lead merges TOOL_SPECS into
app/tools/registry.py's TOOL_REGISTRY as `ToolSpec(**spec)` (kept as plain
dicts here so this module never imports the registry, avoiding a cycle)."""

from typing import Any

import httpx
from app.services.platform_connectors import (
    ConnectorError,
    decrypt_config,
    lookup_shopify_order,
    lookup_woo_order,
    order_matches,
    summarize_shopify_order,
    summarize_woo_order,
)
from app.tools.context import ToolContext
from sqlalchemy import select

from packages.database.models import Customer
from packages.database.models.platform import IntegrationConnector


async def lookup_order(
    ctx: ToolContext,
    order_number: str,
    email: str | None = None,
    platform: str | None = None,
) -> dict[str, Any]:
    """Look up an e-commerce order (Shopify or WooCommerce). The caller must be
    verified: the order's email/phone must match `email` or the customer tied to
    this conversation, otherwise no order details are returned."""
    types = [platform] if platform in ("shopify", "woocommerce") else ["shopify", "woocommerce"]
    connectors = (
        (
            await ctx.db.execute(
                select(IntegrationConnector).where(
                    IntegrationConnector.tenant_id == ctx.tenant_id,
                    IntegrationConnector.enabled.is_(True),
                    IntegrationConnector.type.in_(types),
                )
            )
        )
        .scalars()
        .all()
    )
    if not connectors:
        return {"error": "No Shopify or WooCommerce store is connected for this business"}

    customer_phone = customer_email = None
    if ctx.customer_id is not None:
        customer = (
            await ctx.db.execute(
                select(Customer).where(
                    Customer.id == ctx.customer_id, Customer.tenant_id == ctx.tenant_id
                )
            )
        ).scalar_one_or_none()
        if customer is not None:
            customer_phone, customer_email = customer.phone, customer.email
    verify_email = email or customer_email
    if not verify_email and not customer_phone:
        return {"error": "Ask the customer for the email address on the order first"}

    async with httpx.AsyncClient(follow_redirects=False) as client:
        for connector in connectors:
            try:
                config = decrypt_config(connector.config)
                if connector.type == "shopify":
                    order = await lookup_shopify_order(config, order_number, client)
                    if order is None:
                        continue
                    phone = order.get("phone") or (order.get("shipping_address") or {}).get("phone")
                    ok = order_matches(
                        order.get("email") or order.get("contact_email"),
                        phone,
                        email=verify_email,
                        phone=customer_phone,
                    )
                    summary = summarize_shopify_order(order) if ok else None
                else:
                    order = await lookup_woo_order(config, order_number, client)
                    if order is None:
                        continue
                    billing = order.get("billing") or {}
                    ok = order_matches(
                        billing.get("email"),
                        billing.get("phone"),
                        email=verify_email,
                        phone=customer_phone,
                    )
                    summary = summarize_woo_order(order) if ok else None
            except ConnectorError as exc:
                return {"error": f"Order lookup failed: {exc}"}
            if not ok:
                return {"found": False, "error": "That order does not match the details provided"}
            return {"found": True, **summary}
    return {"found": False, "error": "No order with that number was found"}


TOOL_SPECS: dict[str, dict[str, Any]] = {
    "lookup_order": {
        "name": "lookup_order",
        "description": "Look up the status, items and tracking of an online-store order "
        "(Shopify or WooCommerce). Requires the order number and the email on the order "
        "for verification.",
        "parameters": {
            "type": "object",
            "properties": {
                "order_number": {"type": "string", "description": "Order number, e.g. 1001"},
                "email": {
                    "type": "string",
                    "description": "Email address on the order (for verification)",
                },
                "platform": {"type": "string", "enum": ["shopify", "woocommerce"]},
            },
            "required": ["order_number"],
        },
        "handler": lookup_order,
    }
}
