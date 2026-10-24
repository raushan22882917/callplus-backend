"""Prepaid wallet: balance and rates, top-ups through Razorpay Checkout, the ledger and the
GST invoices. See services/wallet.py for how usage is charged."""

import uuid

from app.core.audit import log_audit_event
from app.core.config import get_settings
from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.integrations import razorpay_client
from app.schemas.wallet import BillingProfile, TopupCreate, TopupRead, TopupVerify, TransactionRead
from app.services import plans, wallet
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Tenant, WalletTopup, WalletTransaction

router = APIRouter(prefix="/wallet", tags=["wallet"])
Billing = Depends(require_permission(Permissions.BILLING_MANAGE))


def _can_topup(tenant: Tenant) -> tuple[bool, str | None]:
    tier = plans.effective(tenant)
    if tier.code in ("free", "payg"):
        return True, None
    if tier.code == "legacy":
        return False, "Your workspace has unlimited legacy access. Contact support to move to pay-as-you-go."
    return False, f"You are on the {tier.name} plan, which includes its own minutes. The wallet is for pay-as-you-go workspaces."


async def _summary(db: AsyncSession, tenant: Tenant) -> dict:
    await wallet.settle(db, tenant.id)
    w = await wallet.get_wallet(db, tenant.id)
    can, why_not = _can_topup(tenant)
    inbound, outbound = (w.inbound_rate_paise, w.outbound_rate_paise) if w else wallet.rates_for_topup(0)
    balance = w.balance_paise if w else 0
    return {
        "balance_paise": balance,
        "low": balance < (w.low_balance_paise if w else wallet.LOW_BALANCE_PAISE),
        "active": bool(w) and plans.effective(tenant).code == "payg",
        "inbound_rate_paise": inbound,
        "outbound_rate_paise": outbound,
        "minutes_left_inbound": max(balance, 0) // max(inbound, 1),
        "minutes_left_outbound": max(balance, 0) // max(outbound, 1),
        "whatsapp_message_paise": wallet.WHATSAPP_MESSAGE_PAISE,
        "sms_paise": wallet.SMS_PAISE,
        "gst_percent": wallet.GST_PERCENT,
        "min_topup_paise": wallet.TOPUP_MIN_PAISE,
        "max_topup_paise": wallet.TOPUP_MAX_PAISE,
        "rate_table": wallet.rate_table(),
        "can_topup": can,
        "cannot_topup_reason": why_not,
        "billing_profile": {
            "billing_name": w.billing_name if w else None,
            "gstin": w.gstin if w else None,
            "state_code": w.state_code if w else None,
        },
    }


@router.get("")
async def get_wallet(ctx: RequestContext = Billing, db: AsyncSession = Depends(get_db)) -> dict:
    tenant = await db.get(Tenant, ctx.tenant_id)
    out = await _summary(db, tenant)
    await db.commit()
    return out


@router.put("/billing-profile")
async def save_billing_profile(
    payload: BillingProfile, ctx: RequestContext = Billing, db: AsyncSession = Depends(get_db)
) -> dict:
    gstin = None
    state = payload.state_code
    if payload.gstin:
        try:
            gstin = wallet.validate_gstin(payload.gstin)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        if state and state != gstin[:2]:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "The state code does not match the GSTIN"
            )
        state = gstin[:2]
    w = await wallet.ensure_wallet(db, ctx.tenant_id)
    w.billing_name, w.gstin, w.state_code = payload.billing_name.strip(), gstin, state
    await log_audit_event(
        db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="wallet.billing_profile_saved",
        resource_type="wallet", resource_id=w.id, extra={"gstin": bool(gstin)},
    )
    tenant = await db.get(Tenant, ctx.tenant_id)
    out = await _summary(db, tenant)
    await db.commit()
    return out


@router.post("/topups", status_code=status.HTTP_201_CREATED)
async def create_topup(
    payload: TopupCreate, ctx: RequestContext = Billing, db: AsyncSession = Depends(get_db)
) -> dict:
    tenant = await db.get(Tenant, ctx.tenant_id)
    can, why_not = _can_topup(tenant)
    if not can:
        raise HTTPException(status.HTTP_409_CONFLICT, why_not)
    amount = payload.amount_rupees * 100
    w = await wallet.ensure_wallet(db, ctx.tenant_id)
    if not w.billing_name:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Add your billing name (and GSTIN if you have one) first"
        )
    settings = get_settings()
    tax = wallet.gst_split(amount, w.state_code, settings.seller_state_code or None)
    inbound, outbound = wallet.rates_for_topup(amount)
    receipt = f"wt_{uuid.uuid4().hex[:24]}"
    try:
        order = await razorpay_client.create_order(
            amount_paise=tax["total"], receipt=receipt, notes={"tenant_id": str(ctx.tenant_id)}
        )
    except razorpay_client.RazorpayAPIError as exc:
        raise HTTPException(status.HTTP_424_FAILED_DEPENDENCY, str(exc)) from exc
    topup = WalletTopup(
        tenant_id=ctx.tenant_id,
        provider_order_id=order["id"],
        status="created",
        amount_paise=amount,
        cgst_paise=tax["cgst"],
        sgst_paise=tax["sgst"],
        igst_paise=tax["igst"],
        total_paise=tax["total"],
        inbound_rate_paise=inbound,
        outbound_rate_paise=outbound,
        billing_name=w.billing_name,
        gstin=w.gstin,
        state_code=w.state_code,
        seller_state_code=settings.seller_state_code or None,
        created_by_user_id=ctx.user_id,
    )
    db.add(topup)
    await log_audit_event(
        db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="wallet.topup_started",
        resource_type="wallet_topup", resource_id=None, extra={"amount_paise": amount},
    )
    await db.commit()
    return {
        "topup_id": str(topup.id),
        "order_id": order["id"],
        "key_id": settings.razorpay_key_id,
        "amount_paise": tax["total"],
        "credit_paise": amount,
        "gst_paise": tax["gst"],
        "inbound_rate_paise": inbound,
        "outbound_rate_paise": outbound,
        "currency": "INR",
    }


async def _own_topup(db: AsyncSession, tenant_id: uuid.UUID, topup_id: uuid.UUID) -> WalletTopup:
    topup = (
        await db.execute(
            select(WalletTopup).where(WalletTopup.id == topup_id, WalletTopup.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if topup is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Top-up not found")
    return topup


@router.post("/topups/{topup_id}/verify")
async def verify_topup(
    topup_id: uuid.UUID,
    payload: TopupVerify,
    ctx: RequestContext = Billing,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Called by the browser right after Razorpay Checkout succeeds. The signature proves
    Razorpay confirmed this payment for this order; the webhook does the same if the
    browser never gets here."""
    topup = await _own_topup(db, ctx.tenant_id, topup_id)
    try:
        valid = razorpay_client.verify_checkout_signature(
            order_id=topup.provider_order_id, payment_id=payload.payment_id, signature=payload.signature
        )
    except razorpay_client.RazorpayAPIError as exc:
        raise HTTPException(status.HTTP_424_FAILED_DEPENDENCY, str(exc)) from exc
    if not valid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The payment could not be verified")
    if await wallet.credit_topup(db, topup, payload.payment_id):
        await log_audit_event(
            db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="wallet.topped_up",
            resource_type="wallet_topup", resource_id=topup.id, extra={"amount_paise": topup.amount_paise},
        )
    tenant = await db.get(Tenant, ctx.tenant_id)
    out = await _summary(db, tenant)
    await db.commit()
    return out


@router.get("/transactions", response_model=list[TransactionRead])
async def list_transactions(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Billing,
    db: AsyncSession = Depends(get_db),
) -> list[WalletTransaction]:
    await wallet.settle(db, ctx.tenant_id)
    await db.commit()
    rows = await db.execute(
        select(WalletTransaction)
        .where(WalletTransaction.tenant_id == ctx.tenant_id)
        .order_by(WalletTransaction.created_at.desc(), WalletTransaction.id)
        .limit(limit)
        .offset(offset)
    )
    return list(rows.scalars().all())


@router.get("/topups", response_model=list[TopupRead])
async def list_topups(ctx: RequestContext = Billing, db: AsyncSession = Depends(get_db)) -> list[WalletTopup]:
    rows = await db.execute(
        select(WalletTopup)
        .where(WalletTopup.tenant_id == ctx.tenant_id, WalletTopup.status == "paid")
        .order_by(WalletTopup.paid_at.desc())
    )
    return list(rows.scalars().all())


@router.get("/topups/{topup_id}/invoice", response_class=HTMLResponse)
async def topup_invoice(
    topup_id: uuid.UUID, ctx: RequestContext = Billing, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    topup = await _own_topup(db, ctx.tenant_id, topup_id)
    if topup.status != "paid":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No invoice yet: this top-up was not paid")
    s = get_settings()
    return HTMLResponse(
        wallet.invoice_html(
            topup, {"name": s.seller_legal_name, "gstin": s.seller_gstin, "address": s.seller_address}
        )
    )
