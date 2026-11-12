"""Creating a workspace and its owner. One place, so the password form and 'Continue with Google' cannot drift."""

import re
import secrets
from datetime import UTC, datetime

from app.core.config import get_settings
from app.core.security import hash_password
from app.services import plans
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Role, Tenant, TenantUser, User


class SystemRolesMissing(Exception):
    """The migrations that seed the OWNER role have not run."""


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:80] or "workspace"


async def unique_slug(db: AsyncSession, text: str) -> str:
    base = slugify(text)
    candidate = base
    while (await db.execute(select(Tenant.id).where(Tenant.slug == candidate))).first():
        candidate = f"{base[:90]}-{secrets.token_hex(2)}"
    return candidate


async def create_workspace(
    db: AsyncSession,
    *,
    company_name: str,
    slug: str,
    full_name: str,
    email: str,
    hashed_password: str,
) -> tuple[Tenant, User, TenantUser]:
    """Adds the workspace, its first user (owner, email already verified) and the membership, and flushes.
    A taken slug or email raises IntegrityError for the caller to turn into a message."""
    owner_role = (
        await db.execute(select(Role).where(Role.tenant_id.is_(None), Role.name == "OWNER"))
    ).scalar_one_or_none()
    if owner_role is None:
        raise SystemRolesMissing()
    tier = get_settings().signup_plan_tier
    tenant = Tenant(name=company_name, slug=slug, plan_tier=tier if tier in plans.TIERS else "free")
    user = User(
        email=email.lower(), hashed_password=hashed_password, full_name=full_name, email_verified_at=datetime.now(UTC)
    )
    db.add_all([tenant, user])
    await db.flush()
    membership = TenantUser(tenant_id=tenant.id, user_id=user.id, role_id=owner_role.id)
    db.add(membership)
    await db.flush()
    return tenant, user, membership


async def create_from_identity(
    db: AsyncSession, *, email: str, name: str | None, company_name: str | None
) -> tuple[Tenant, User, TenantUser]:
    """A first sign-in with Google or Microsoft: the provider has verified the email, so the person gets a
    workspace straight away. There is no password; they can set one with 'Forgot password' if they want one."""
    local = email.split("@")[0]
    full_name = (name or "").strip()[:255] or local
    company = (company_name or "").strip()[:255] or f"{full_name.split()[0]}'s workspace"
    return await create_workspace(
        db,
        company_name=company,
        slug=await unique_slug(db, company),
        full_name=full_name,
        email=email,
        hashed_password=hash_password(secrets.token_urlsafe(32)),
    )

