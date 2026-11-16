"""Whether anyone has proved they own an account's email address.

Signing up with a password does not ask for a confirmation email, and neither does being invited with a
temporary password. So on those accounts the password was chosen by someone who has not shown they can
read the inbox, and `users.email_verified_at` stays empty until one of these happens:

  - they sign in with Google or Microsoft (the provider vouches for the address);
  - they finish "Forgot password" (the link went to the inbox).

A workspace's own SSO does not count: its admin chose that provider, and it can name any address.

The first proof matters. Someone can register another person's address before that person arrives, then
keep the password. When the real owner later signs in through a provider, everything set up before the
proof is thrown away: the password is replaced with one nobody knows and every session is ended. Without
this the earlier registrant would still be able to sign in to the owner's account.
"""

import secrets
from datetime import UTC, datetime

from app.core.security import hash_password
from app.services.user_sessions import revoke_others
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import User


def is_proven(user: User) -> bool:
    return user.email_verified_at is not None


def mark_proven(user: User) -> None:
    if user.email_verified_at is None:
        user.email_verified_at = datetime.now(UTC)


async def claim_for_proven_owner(db: AsyncSession, user: User) -> bool:
    """Call when an identity provider has just vouched for this user's email. If nobody had proved the
    address before, drop the password and sessions that predate the proof. Returns True if it did so
    (worth an audit entry); False if the address was already proven and nothing changed."""
    if is_proven(user):
        return False
    user.hashed_password = hash_password(secrets.token_urlsafe(32))
    await revoke_others(db, user.id, keep=None)
    mark_proven(user)
    return True
