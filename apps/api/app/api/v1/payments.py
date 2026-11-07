import json
from datetime import UTC, datetime

from app.core.audit import log_audit_event
from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.core.repository import TenantScopedRepository
from app.integrations import paypal_client
from app.integrations.razorpay_client import (
    RazorpayAPIError,
    create_payment_link,
    verify_webhook_signature,
)
from app.schemas.payment import PaymentLinkCreate, PaymentLinkRead
from app.services import plans
from app.services import wallet as wallet_service
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    Customer,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentLink,
    PaymentLinkStatus,
    PaymentStatus,
    Plan,
    Subscription,
    SubscriptionStatus,
    Tenant,
    WalletTopup,
)

router = APIRouter(tags=["payments"])


class PaymentLinkRepository(TenantScopedRepository[PaymentLink]):
    model = PaymentLink


class PaymentRepository(TenantScopedRepository[Payment]):
    model = Payment


@router.get("/payment-links", response_model=list[PaymentLinkRead])
async def list_payment_links(
    ctx: RequestContext = Depends(require_permission(Permissions.PAYMENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[PaymentLink]:
    return await PaymentLinkRepository(db, ctx.tenant_id).list()


@router.post("/payment-links", response_model=PaymentLinkRead, status_code=status.HTTP_201_CREATED)
async def create_payment_link_endpoint(
    payload: PaymentLinkCreate,
    ctx: RequestContext = Depends(require_permission(Permissions.PAYMENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> PaymentLink:
    customer_name = customer_phone = None
    if payload.customer_id is not None:
        customer = await db.get(Customer, payload.customer_id)
        if customer is None or customer.tenant_id != ctx.tenant_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Customer not found")
        customer_name, customer_phone = customer.name, customer.phone

    try:
        response = await create_payment_link(
            amount_paise=int(round(payload.amount * 100)),
            currency=payload.currency,
            description=payload.description,
            customer_name=customer_name,
            customer_contact=customer_phone,
        )
    except RazorpayAPIError as exc:
        raise HTTPException(status.HTTP_424_FAILED_DEPENDENCY, str(exc)) from exc

    link = await PaymentLinkRepository(db, ctx.tenant_id).create(
        customer_id=payload.customer_id,
        provider_payment_link_id=response["id"],
        amount_paise=response["amount"],
        currency=response.get("currency", "INR"),
        description=payload.description,
        short_url=response["short_url"],
        status=PaymentLinkStatus(response.get("status", "created")),
    )
    await db.commit()
    await db.refresh(link)
    return link


@router.post("/webhooks/payment/razorpay")
async def razorpay_webhook(request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature")
    if not verify_webhook_signature(raw_body, signature):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid webhook signature")

    body = await request.json()
    event = body.get("event")
    payload = body.get("payload", {})

    # Only `payment_link.paid` is handled: it's the one event we can reliably
    # attribute to a tenant, since `payload.payment_link.entity.id` matches
    # what we stored as provider_payment_link_id. A bare `payment.captured`
    # references its order_id, not the payment link id — attributing that
    # back to a tenant wasn't confirmed against live Razorpay docs (see
    # app/integrations/razorpay_client.py docstring), so it's deliberately
    # not handled here rather than guessing at a field mapping.
    if event == "payment_link.paid":
        await _handle_payment_link_paid(
            db,
            link_entity=payload.get("payment_link", {}).get("entity", {}),
            payment_entity=payload.get("payment", {}).get("entity", {}),
        )
    elif event in ("payment.captured", "order.paid"):
        await _handle_wallet_payment(db, payment_entity=payload.get("payment", {}).get("entity", {}))
    elif event in (
        "subscription.activated",
        "subscription.charged",
        "subscription.cancelled",
        "subscription.completed",
        "subscription.halted",
    ):
        await _handle_subscription_event(
            db,
            event=event,
            subscription_entity=payload.get("subscription", {}).get("entity", {}),
            payment_entity=payload.get("payment", {}).get("entity", {}),
        )

    await db.commit()
    return {"status": "ok"}


@router.post("/webhooks/payment/paypal")
async def paypal_webhook(request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    raw = await request.body()
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid JSON") from exc
    if not await paypal_client.verify_webhook(request.headers, event):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid webhook signature")
    kind = event.get("event_type")
    resource = event.get("resource") or {}
    if kind == "CHECKOUT.ORDER.APPROVED":
        order_id = resource.get("id")
        if order_id:
            try:
                order = await paypal_client.capture_order(order_id)
            except paypal_client.PayPalAPIError as exc:
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
            await _credit_paypal_order(db, order)
    elif kind == "PAYMENT.CAPTURE.COMPLETED":
        await _credit_paypal_capture(db, resource)
    await db.commit()
    return {"status": "ok"}


async def _credit_paypal_order(db: AsyncSession, order: dict) -> None:
    captured = paypal_client.capture_from_order(order)
    if captured is None or captured[2] != "INR":
        return
    payment_id, paid_paise, _currency = captured
    if await wallet_service.credit_paid_order(
        db,
        order_id=order.get("id", ""),
        payment_id=payment_id,
        paid_paise=paid_paise,
        provider="paypal",
    ):
        topup = (
            await db.execute(
                select(WalletTopup).where(WalletTopup.provider_order_id == order.get("id"))
            )
        ).scalar_one_or_none()
        if topup is not None:
            await log_audit_event(
                db, tenant_id=topup.tenant_id, actor_user_id=None, action="wallet.topped_up",
                resource_type="wallet_topup", resource_id=topup.id,
                extra={"amount_paise": topup.amount_paise, "via": "paypal_webhook"},
            )


async def _credit_paypal_capture(db: AsyncSession, resource: dict) -> None:
    related = (resource.get("supplementary_data") or {}).get("related_ids") or {}
    order_id = related.get("order_id")
    payment_id = resource.get("id")
    paid = paypal_client.paise_from_amount(resource.get("amount") or {})
    currency = (resource.get("amount") or {}).get("currency_code")
    if not order_id or not payment_id or paid is None or currency != "INR":
        return
    if await wallet_service.credit_paid_order(
        db, order_id=order_id, payment_id=payment_id, paid_paise=paid, provider="paypal"
    ):
        topup = (
            await db.execute(select(WalletTopup).where(WalletTopup.provider_order_id == order_id))
        ).scalar_one_or_none()
        if topup is not None:
            await log_audit_event(
                db, tenant_id=topup.tenant_id, actor_user_id=None, action="wallet.topped_up",
                resource_type="wallet_topup", resource_id=topup.id,
                extra={"amount_paise": topup.amount_paise, "via": "paypal_webhook"},
            )


async def _handle_wallet_payment(db: AsyncSession, *, payment_entity: dict) -> None:
    """A wallet top-up paid: credit it if the browser has not already confirmed it. Only an
    order we created, for exactly the amount we asked for, is credited."""
    order_id, payment_id = payment_entity.get("order_id"), payment_entity.get("id")
    if not order_id or not payment_id:
        return
    topup = (
        await db.execute(select(WalletTopup).where(WalletTopup.provider_order_id == order_id))
    ).scalar_one_or_none()
    if topup is None or topup.provider != "razorpay":
        return
    if payment_entity.get("amount") != topup.total_paise:
        return
    if await wallet_service.credit_topup(db, topup, payment_id):
        await log_audit_event(
            db, tenant_id=topup.tenant_id, actor_user_id=None, action="wallet.topped_up",
            resource_type="wallet_topup", resource_id=topup.id,
            extra={"amount_paise": topup.amount_paise, "via": "webhook"},
        )


async def _handle_payment_link_paid(
    db: AsyncSession, *, link_entity: dict, payment_entity: dict
) -> None:
    provider_link_id = link_entity.get("id")
    if not provider_link_id:
        return
    result = await db.execute(
        select(PaymentLink).where(PaymentLink.provider_payment_link_id == provider_link_id)
    )
    link = result.scalar_one_or_none()
    if link is None:
        return
    link.status = PaymentLinkStatus.PAID

    payment_id = payment_entity.get("id")
    if not payment_id:
        return
    existing = await db.execute(select(Payment).where(Payment.provider_payment_id == payment_id))
    if existing.scalar_one_or_none() is not None:
        return  # webhook retry — already recorded

    db.add(
        Payment(
            tenant_id=link.tenant_id,
            payment_link_id=link.id,
            provider_payment_id=payment_id,
            amount_paise=payment_entity.get("amount", link.amount_paise),
            currency=payment_entity.get("currency", link.currency),
            status=PaymentStatus.CAPTURED,
            method=payment_entity.get("method"),
        )
    )


_SUBSCRIPTION_EVENT_STATUS = {
    "subscription.activated": SubscriptionStatus.ACTIVE,
    "subscription.charged": SubscriptionStatus.ACTIVE,
    "subscription.cancelled": SubscriptionStatus.CANCELLED,
    "subscription.completed": SubscriptionStatus.COMPLETED,
    "subscription.halted": SubscriptionStatus.PAST_DUE,
}


async def _sync_tenant_tier(db: AsyncSession, subscription: Subscription, event: str) -> None:
    """Moves the workspace between plan tiers as its subscription changes.

    Paid (activated/charged): the tier of the subscribed plan. Ended (cancelled/
    completed): back to Free, but only if the tenant is still on that plan's tier,
    so a hand-granted tier (an Enterprise deal) is never downgraded by a stale
    subscription. Past due (halted) keeps the tier while Razorpay retries."""
    plan = await db.get(Plan, subscription.plan_id)
    subscribed_tier = plans.tier_for_plan_code(plan.code) if plan else None
    tenant = await db.get(Tenant, subscription.tenant_id)
    if subscribed_tier is None or tenant is None:
        return

    if event in ("subscription.activated", "subscription.charged"):
        new_tier = subscribed_tier
    elif event in ("subscription.cancelled", "subscription.completed"):
        if tenant.plan_tier != subscribed_tier:
            return
        new_tier = "free"
    else:
        return

    if tenant.plan_tier == new_tier:
        return
    previous = tenant.plan_tier
    tenant.plan_tier = new_tier
    await log_audit_event(
        db,
        tenant_id=tenant.id,
        actor_user_id=None,
        action="plan.changed",
        resource_type="tenant",
        resource_id=tenant.id,
        extra={"from": previous, "to": new_tier, "event": event, "plan_code": plan.code},
    )


async def _handle_subscription_event(
    db: AsyncSession, *, event: str, subscription_entity: dict, payment_entity: dict
) -> None:
    provider_subscription_id = subscription_entity.get("id")
    if not provider_subscription_id:
        return
    result = await db.execute(
        select(Subscription).where(
            Subscription.provider_subscription_id == provider_subscription_id
        )
    )
    subscription = result.scalar_one_or_none()
    if subscription is None:
        return

    subscription.status = _SUBSCRIPTION_EVENT_STATUS[event]
    current_end = subscription_entity.get("current_end")
    if current_end:
        subscription.current_period_end = datetime.fromtimestamp(current_end, tz=UTC)
    await _sync_tenant_tier(db, subscription, event)

    if event == "subscription.charged":
        payment_id = payment_entity.get("id")
        if not payment_id:
            return
        existing = await db.execute(
            select(Invoice).where(Invoice.provider_payment_id == payment_id)
        )
        if existing.scalar_one_or_none() is not None:
            return  # webhook retry — already recorded

        db.add(
            Invoice(
                tenant_id=subscription.tenant_id,
                subscription_id=subscription.id,
                provider_payment_id=payment_id,
                amount_paise=payment_entity.get("amount", 0),
                currency=payment_entity.get("currency", "INR"),
                status=InvoiceStatus.PAID,
                issued_at=datetime.now(UTC),
                paid_at=datetime.now(UTC),
            )
        )
