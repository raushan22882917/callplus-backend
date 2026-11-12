"""Channel packs: Call, WhatsApp, and Email, bought one at a time or all three together.

Every route needs the billing permission and only ever touches the caller's own workspace. The browser says
which packs it wants and how to pay; the amount, tax and allowances are worked out in services/packs.py.
See that module for how a payment becomes an allowance."""

import uuid

from app.core.audit import log_audit_event
from app.core.config import get_settings
from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.integrations import paypal_client, razorpay_client
from app.schemas.packs import PackCode, PackPayPalCapture, PackPurchaseCreate, PackPurchaseRead, PackVerify
from app.services import packs, plans, wallet
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import PackPurchase, Tenant

router = APIRouter(prefix="/packs", tags=["packs"])
Billing = Depends(require_permission(Permissions.BILLING_MANAGE))


async def _own(db: AsyncSession, tenant_id: uuid.UUID, purchase_id: uuid.UUID) -> PackPurchase:
    row = (
        await db.execute(
            select(PackPurchase).where(PackPurchase.id == purchase_id, PackPurchase.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Purchase not found")
    return row


async def _buyer_state(db: AsyncSession, tenant_id: uuid.UUID) -> str | None:
    w = await wallet.get_wallet(db, tenant_id)
    return w.state_code if w else None


@router.get("/catalogue")
async def catalogue(ctx: RequestContext = Billing, db: AsyncSession = Depends(get_db)) -> dict:
    """The packs and the bundle, with GST worked out for this workspace's state when it is known."""
    tenant = await db.get(Tenant, ctx.tenant_id)
    out = packs.catalogue(await _buyer_state(db, ctx.tenant_id))
    out["why_not"] = packs.can_buy(tenant)
    out["can_buy"] = out["why_not"] is None
    return out


@router.get("/quote")
async def quote(
    items: list[PackCode] = Query(min_length=1, max_length=3),
    state_code: str | None = Query(default=None, pattern=r"^\d{2}$"),
    ctx: RequestContext = Billing,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """What this choice costs, from the same rules the order uses, so the page never works out a price itself."""
    try:
        q = packs.quote(list(items))
    except packs.PackError as exc:
        raise HTTPException(exc.http_status, exc.message) from exc
    state = state_code or await _buyer_state(db, ctx.tenant_id)
    tax = wallet.gst_split(q["amount_paise"], state, get_settings().seller_state_code or None)
    return {
        "items": q["items"], "bundle": q["bundle"], "list_price_paise": q["list_price_paise"],
        "discount_paise": q["discount_paise"], "price_paise": q["amount_paise"], "gst_paise": tax["gst"],
        "total_paise": tax["total"], "gst_percent": wallet.GST_PERCENT, "period_days": q["period_days"],
    }


@router.get("")
async def my_packs(ctx: RequestContext = Billing, db: AsyncSession = Depends(get_db)) -> dict:
    """What the workspace holds now: expiry and use per pack."""
    tenant = await db.get(Tenant, ctx.tenant_id)
    held = packs.active(tenant)
    tier = plans.effective(tenant)
    snapshot = (
        await plans.entitlements(db, tenant)
        if tier.code == "packs"
        else {"tier": tier.code, "name": tier.name, "meters": [], "packs": {}}
    )
    await db.commit()
    return {
        "on_packs": tier.code == "packs",
        "held": {c: {"expires_at": e["end"].isoformat(), "started_at": e["start"].isoformat()} for c, e in held.items()},
        "meters": snapshot.get("meters", []),
        "why_not": packs.can_buy(tenant),
    }


@router.post("/purchases", status_code=status.HTTP_201_CREATED)
async def create_purchase(
    payload: PackPurchaseCreate, ctx: RequestContext = Billing, db: AsyncSession = Depends(get_db)
) -> dict:
    tenant = await db.get(Tenant, ctx.tenant_id)
    await plans.ensure_owner_verified(db, tenant)
    why_not = packs.can_buy(tenant)
    if why_not:
        raise HTTPException(status.HTTP_409_CONFLICT, why_not)
    if await packs.too_many_open_orders(db, ctx.tenant_id):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many unpaid orders. Finish or wait before starting another.")
    try:
        quote = packs.quote(payload.items)
    except packs.PackError as exc:
        raise HTTPException(exc.http_status, exc.message) from exc

    w = await wallet.get_wallet(db, ctx.tenant_id)  # a saved billing profile fills in when the request names none
    if payload.billing_name:
        name, raw_gstin, state = payload.billing_name.strip(), payload.gstin, payload.state_code
    else:
        name, raw_gstin, state = ((w.billing_name or "").strip(), w.gstin, w.state_code) if w else ("", None, None)
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Add your billing name (and GSTIN if you have one) first")
    gstin = None
    if raw_gstin:
        try:
            gstin = wallet.validate_gstin(raw_gstin)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        if state and state != gstin[:2]:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The state code does not match the GSTIN")
        state = gstin[:2]

    settings = get_settings()
    tax = wallet.gst_split(quote["amount_paise"], state, settings.seller_state_code or None)
    receipt = f"pk_{uuid.uuid4().hex[:24]}"
    try:
        if payload.provider == "paypal":
            return_url, cancel_url = paypal_client.checkout_urls("packs")
            order = await paypal_client.create_order(
                amount_paise=tax["total"], receipt=receipt, custom_id=str(ctx.tenant_id),
                return_url=return_url, cancel_url=cancel_url,
            )
        else:
            order = await razorpay_client.create_order(
                amount_paise=tax["total"], receipt=receipt, notes={"tenant_id": str(ctx.tenant_id), "kind": "packs"}
            )
    except (razorpay_client.RazorpayAPIError, paypal_client.PayPalAPIError) as exc:
        raise HTTPException(status.HTTP_424_FAILED_DEPENDENCY, str(exc)) from exc

    purchase = PackPurchase(
        tenant_id=ctx.tenant_id, provider=payload.provider, provider_order_id=order["id"], status="created",
        items=quote["items"], bundle=quote["bundle"], allowances=quote["allowances"], period_days=quote["period_days"],
        amount_paise=quote["amount_paise"], discount_paise=quote["discount_paise"],
        cgst_paise=tax["cgst"], sgst_paise=tax["sgst"], igst_paise=tax["igst"], total_paise=tax["total"],
        charged_cents=order.get("charged_cents") if payload.provider == "paypal" else None,
        fx_inr_per_usd=order.get("fx_inr_per_usd") if payload.provider == "paypal" else None,
        billing_name=name, gstin=gstin, state_code=state, seller_state_code=settings.seller_state_code or None,
        created_by_user_id=ctx.user_id,
    )
    db.add(purchase)
    await db.flush()
    await log_audit_event(
        db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="pack.purchase_started",
        resource_type="pack_purchase", resource_id=purchase.id,
        extra={"items": quote["items"], "total_paise": tax["total"], "provider": payload.provider},
    )
    await db.commit()
    return {
        "purchase_id": str(purchase.id),
        "provider": payload.provider,
        "order_id": order["id"],
        "key_id": settings.razorpay_key_id if payload.provider == "razorpay" else None,
        "client_id": settings.paypal_client_id if payload.provider == "paypal" else None,
        "approve_url": order.get("approve_url") if payload.provider == "paypal" else None,
        "items": quote["items"],
        "bundle": quote["bundle"],
        "list_price_paise": quote["list_price_paise"],
        "discount_paise": quote["discount_paise"],
        "price_paise": quote["amount_paise"],
        "gst_paise": tax["gst"],
        "amount_paise": tax["total"],
        "currency": "INR",
        "paypal_amount": order.get("value") if payload.provider == "paypal" else None,
        "paypal_currency": order.get("currency") if payload.provider == "paypal" else None,
    }


async def _applied(db: AsyncSession, ctx: RequestContext, purchase: PackPurchase, via: str) -> dict:
    await log_audit_event(
        db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="pack.purchased",
        resource_type="pack_purchase", resource_id=purchase.id,
        extra={"items": purchase.items, "total_paise": purchase.total_paise, "via": via},
    )
    await db.commit()
    tenant = await db.get(Tenant, ctx.tenant_id)
    return {
        "status": purchase.status,
        "invoice_number": purchase.invoice_number,
        "held": {c: {"expires_at": e["end"].isoformat()} for c, e in packs.active(tenant).items()},
    }


@router.post("/purchases/{purchase_id}/verify")
async def verify_purchase(
    purchase_id: uuid.UUID, payload: PackVerify, ctx: RequestContext = Billing, db: AsyncSession = Depends(get_db)
) -> dict:
    """The browser calls this right after Razorpay Checkout succeeds. The signature proves Razorpay confirmed this
    payment for this order; the webhook does the same if the browser never gets here."""
    purchase = await _own(db, ctx.tenant_id, purchase_id)
    if purchase.provider == "paypal":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Confirm a PayPal purchase with the capture endpoint")
    try:
        valid = razorpay_client.verify_checkout_signature(
            order_id=purchase.provider_order_id, payment_id=payload.payment_id, signature=payload.signature
        )
    except razorpay_client.RazorpayAPIError as exc:
        raise HTTPException(status.HTTP_424_FAILED_DEPENDENCY, str(exc)) from exc
    if not valid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The payment could not be verified")
    if await packs.credit_purchase(db, purchase, payload.payment_id):
        return await _applied(db, ctx, purchase, "checkout")
    await db.commit()  # already applied by the webhook
    return await _applied_again(db, ctx, purchase)


async def _applied_again(db: AsyncSession, ctx: RequestContext, purchase: PackPurchase) -> dict:
    await db.refresh(purchase)
    tenant = await db.get(Tenant, ctx.tenant_id)
    return {
        "status": purchase.status,
        "invoice_number": purchase.invoice_number,
        "held": {c: {"expires_at": e["end"].isoformat()} for c, e in packs.active(tenant).items()},
    }


async def _capture_paypal(db: AsyncSession, ctx: RequestContext, purchase: PackPurchase) -> dict:
    if purchase.provider != "paypal":
        raise HTTPException(status.HTTP_409_CONFLICT, "This purchase is paid with Razorpay")
    try:
        order = await paypal_client.capture_order(purchase.provider_order_id)
    except paypal_client.PayPalAPIError as exc:
        raise HTTPException(status.HTTP_424_FAILED_DEPENDENCY, str(exc)) from exc
    payment_id = paypal_client.capture_payment_id(order, purchase.total_paise, expected_cents=purchase.charged_cents)
    if payment_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "PayPal has not completed this payment")
    applied = await packs.credit_paid_order(
        db, order_id=purchase.provider_order_id, payment_id=payment_id, paid_paise=purchase.total_paise, provider="paypal"
    )
    if applied is not None:
        return await _applied(db, ctx, purchase, "paypal")
    await db.commit()
    return await _applied_again(db, ctx, purchase)


@router.post("/paypal/capture")
async def paypal_return(
    payload: PackPayPalCapture, ctx: RequestContext = Billing, db: AsyncSession = Depends(get_db)
) -> dict:
    """The billing page calls this when PayPal redirects back with the order id in `token`."""
    purchase = (
        await db.execute(
            select(PackPurchase).where(
                PackPurchase.provider_order_id == payload.order_id,
                PackPurchase.tenant_id == ctx.tenant_id,
                PackPurchase.provider == "paypal",
            )
        )
    ).scalar_one_or_none()
    if purchase is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "PayPal payment not found")
    return await _capture_paypal(db, ctx, purchase)


@router.post("/purchases/{purchase_id}/capture")
async def capture_purchase(
    purchase_id: uuid.UUID, ctx: RequestContext = Billing, db: AsyncSession = Depends(get_db)
) -> dict:
    return await _capture_paypal(db, ctx, await _own(db, ctx.tenant_id, purchase_id))


@router.get("/purchases", response_model=list[PackPurchaseRead])
async def list_purchases(ctx: RequestContext = Billing, db: AsyncSession = Depends(get_db)) -> list[PackPurchase]:
    rows = await db.execute(
        select(PackPurchase).where(PackPurchase.tenant_id == ctx.tenant_id).order_by(PackPurchase.created_at.desc()).limit(100)
    )
    return list(rows.scalars().all())


@router.get("/purchases/{purchase_id}/invoice", response_class=HTMLResponse)
async def invoice(
    purchase_id: uuid.UUID, ctx: RequestContext = Billing, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    purchase = await _own(db, ctx.tenant_id, purchase_id)
    if purchase.status != "paid":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No invoice yet: this purchase was not paid")
    s = get_settings()
    return HTMLResponse(
        packs.invoice_html(purchase, {"name": s.seller_legal_name, "gstin": s.seller_gstin, "address": s.seller_address})
    )
