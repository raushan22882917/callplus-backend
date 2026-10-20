"""Email the workspace owners about security-relevant changes (new or rotated
API keys, and so on). Best effort: an alert failing never blocks the action."""

import html
import logging
import uuid

from app.integrations.email_client import send_email
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import MembershipStatus, Role, TenantUser, User

logger = logging.getLogger(__name__)


async def owner_emails(db: AsyncSession, tenant_id: uuid.UUID) -> list[str]:
    rows = await db.execute(
        select(User.email)
        .join(TenantUser, TenantUser.user_id == User.id)
        .join(Role, Role.id == TenantUser.role_id)
        .where(
            TenantUser.tenant_id == tenant_id,
            TenantUser.status == MembershipStatus.ACTIVE,
            User.is_active.is_(True),
            Role.name == "OWNER",
        )
    )
    return sorted({email for (email,) in rows.all()})


async def notify_owners(
    db: AsyncSession, tenant_id: uuid.UUID, *, subject: str, message: str
) -> None:
    try:
        body = f"<p>{html.escape(message)}</p><p>If you did not expect this, review your API keys and sessions in Settings → Security.</p>"
        for email in await owner_emails(db, tenant_id):
            await send_email(to_email=email, subject=subject, html_body=body)
    except Exception:
        logger.warning("security alert %r failed for tenant %s", subject, tenant_id, exc_info=True)
