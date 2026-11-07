"""Release phone numbers this platform bought, so Twilio stops billing us.

A number the tenant brought themselves (`platform_owned` is false) is never
released: unregistering it only stops us routing calls to it.
"""

import logging
from datetime import UTC, datetime, timedelta

from app.integrations.credentials import get_twilio_credentials
from app.integrations.twilio_client import TwilioClientError, release_number
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.base.exceptions import TwilioException

from packages.database.models import Call, PhoneNumber, Tenant

logger = logging.getLogger(__name__)

# A free workspace that has not placed or received a call for this long loses
# a platform-bought number. The number is the recurring cost; the minutes are not.
IDLE_AFTER = timedelta(days=14)


class NumberReleaseError(Exception):
    """Twilio refused to release a number we bought. The row must stay, or we
    would forget the provider id and keep paying for a number we can no longer see."""


async def release_if_platform_owned(db: AsyncSession, number: PhoneNumber) -> None:
    if not number.platform_owned or not number.provider_sid or number.provider != "twilio":
        return
    credentials = await get_twilio_credentials(db, number.tenant_id)
    try:
        release_number(provider_sid=number.provider_sid, credentials=credentials)
    except (TwilioClientError, TwilioException) as exc:
        raise NumberReleaseError(f"Twilio could not release {number.e164_number}: {exc}") from exc


async def release_idle_platform_numbers(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Releases platform numbers on free workspaces with no call since the cutoff.

    Returns how many numbers were released. A Twilio failure leaves that number
    in place and continues with the rest.
    """
    moment = now or datetime.now(UTC)
    cutoff = moment - IDLE_AFTER
    rows = (
        await db.execute(
            select(PhoneNumber, Tenant)
            .join(Tenant, Tenant.id == PhoneNumber.tenant_id)
            .where(PhoneNumber.platform_owned.is_(True), Tenant.plan_tier == "free")
        )
    ).all()
    released = 0
    for number, tenant in rows:
        last_call = (
            await db.execute(select(func.max(Call.created_at)).where(Call.tenant_id == tenant.id))
        ).scalar_one()
        activity = last_call or tenant.created_at
        if activity is not None and activity >= cutoff:
            continue
        try:
            await release_if_platform_owned(db, number)
        except NumberReleaseError:
            logger.warning("idle number %s was not released", number.e164_number, exc_info=True)
            continue
        await db.delete(number)
        released += 1
    await db.commit()
    return released
