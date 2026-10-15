"""Response helper that applies a tenant's PII-masking settings (see
app/core/pii.py) to call, transcript and customer API responses."""

from collections.abc import Iterable
from dataclasses import dataclass

from app.core.deps import RequestContext
from app.core.pii import mask_email, mask_phone, mask_text
from app.schemas.call import CallRead
from app.schemas.conversation import MessageRead
from app.schemas.customer import CustomerRead
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.platform import TenantSecuritySettings

ADMIN_ROLES = frozenset({"OWNER", "ADMIN"})


@dataclass(frozen=True)
class MaskingPolicy:
    mask_text: bool = False  # transcripts, summaries, free text; applies to everyone
    mask_phone: bool = False  # phone numbers; non-admin roles only

    @property
    def active(self) -> bool:
        return self.mask_text or self.mask_phone


def policy_for(settings: TenantSecuritySettings | None, ctx: RequestContext) -> MaskingPolicy:
    if settings is None:
        return MaskingPolicy()
    privileged = ctx.is_platform_admin or ctx.api_key_id is not None or ctx.role_name in ADMIN_ROLES
    return MaskingPolicy(
        mask_text=settings.mask_pii,
        mask_phone=settings.mask_phone_non_admin and not privileged,
    )


async def get_masking_policy(db: AsyncSession, ctx: RequestContext) -> MaskingPolicy:
    settings = (
        await db.execute(
            select(TenantSecuritySettings).where(TenantSecuritySettings.tenant_id == ctx.tenant_id)
        )
    ).scalar_one_or_none()
    return policy_for(settings, ctx)


def mask_call_read(call: CallRead, policy: MaskingPolicy) -> CallRead:
    if not policy.active:
        return call
    update: dict = {}
    if policy.mask_phone or policy.mask_text:
        update["from_number"] = mask_phone(call.from_number)
        update["to_number"] = mask_phone(call.to_number)
    if policy.mask_text:
        update["summary"] = mask_text(call.summary)
        update["intent"] = mask_text(call.intent)
        if call.action_items:
            update["action_items"] = [mask_text(i) or "" for i in call.action_items]
    return call.model_copy(update=update)


def mask_message_read(message: MessageRead, policy: MaskingPolicy) -> MessageRead:
    if not policy.mask_text:
        return message
    return message.model_copy(update={"text": mask_text(message.text)})


def mask_customer_read(customer: CustomerRead, policy: MaskingPolicy) -> CustomerRead:
    if not policy.active:
        return customer
    update: dict = {}
    if policy.mask_phone or policy.mask_text:
        update["phone"] = mask_phone(customer.phone)
    if policy.mask_text:
        update["email"] = mask_email(customer.email)
    return customer.model_copy(update=update)


async def masked_calls(db: AsyncSession, ctx: RequestContext, calls: Iterable) -> list[CallRead]:
    policy = await get_masking_policy(db, ctx)
    return [mask_call_read(CallRead.model_validate(c), policy) for c in calls]


async def masked_messages(
    db: AsyncSession, ctx: RequestContext, messages: Iterable
) -> list[MessageRead]:
    policy = await get_masking_policy(db, ctx)
    return [mask_message_read(MessageRead.model_validate(m), policy) for m in messages]


async def masked_customers(
    db: AsyncSession, ctx: RequestContext, customers: Iterable
) -> list[CustomerRead]:
    policy = await get_masking_policy(db, ctx)
    return [mask_customer_read(CustomerRead.model_validate(c), policy) for c in customers]
