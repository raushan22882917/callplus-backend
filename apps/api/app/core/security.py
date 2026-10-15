import enum
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
from app.core.config import get_settings
from pydantic import BaseModel

settings = get_settings()


BCRYPT_MAX_BYTES = 72  # bcrypt ignores (v4) or rejects (v5) anything longer
BCRYPT_ROUNDS = 12


def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if len(encoded) > BCRYPT_MAX_BYTES:
        raise ValueError("password is longer than 72 bytes")
    return bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(password: str, hashed_password: str) -> bool:
    encoded = password.encode("utf-8")
    if len(encoded) > BCRYPT_MAX_BYTES:
        return False
    try:
        return bcrypt.checkpw(encoded, hashed_password.encode("utf-8"))
    except ValueError:  # malformed stored hash
        return False


# Verified against when the account does not exist, so a login attempt for an
# unknown email costs the same as one for a real email (no timing oracle).
_DUMMY_HASH = bcrypt.hashpw(b"timing-equaliser", bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def burn_password_check(password: str) -> None:
    verify_password(password, _DUMMY_HASH)


def password_fingerprint(hashed_password: str) -> str:
    """Short digest of the stored hash. Embedded in password-reset tokens so a
    token stops working the moment the password changes (one-time use)."""
    return hashlib.sha256(hashed_password.encode("utf-8")).hexdigest()[:16]


class TokenType(str, enum.Enum):
    ACCESS = "access"
    REFRESH = "refresh"


class TokenPayload(BaseModel):
    sub: str  # user_id
    membership_id: str
    tenant_id: str
    type: TokenType
    jti: str
    exp: int
    iat: int


def _create_token(
    *,
    user_id: uuid.UUID,
    membership_id: uuid.UUID,
    tenant_id: uuid.UUID,
    token_type: TokenType,
    expires_delta: timedelta,
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "membership_id": str(membership_id),
        "tenant_id": str(tenant_id),
        "type": token_type.value,
        "jti": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_access_token(
    *, user_id: uuid.UUID, membership_id: uuid.UUID, tenant_id: uuid.UUID
) -> str:
    return _create_token(
        user_id=user_id,
        membership_id=membership_id,
        tenant_id=tenant_id,
        token_type=TokenType.ACCESS,
        expires_delta=timedelta(minutes=settings.access_token_expire_minutes),
    )


def create_refresh_token(
    *, user_id: uuid.UUID, membership_id: uuid.UUID, tenant_id: uuid.UUID
) -> str:
    return _create_token(
        user_id=user_id,
        membership_id=membership_id,
        tenant_id=tenant_id,
        token_type=TokenType.REFRESH,
        expires_delta=timedelta(days=settings.refresh_token_expire_days),
    )


class InvalidTokenError(Exception):
    pass


def decode_token(token: str, *, expected_type: TokenType) -> TokenPayload:
    try:
        raw = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(str(exc)) from exc

    payload = TokenPayload.model_validate(raw)
    if payload.type != expected_type:
        raise InvalidTokenError(
            f"expected token type {expected_type.value}, got {payload.type.value}"
        )
    return payload


class PurposeTokenPayload(BaseModel):
    """Single-purpose, user-scoped token — deliberately separate from
    TokenPayload, which requires a membership_id/tenant_id that a
    password-reset or email-verification link doesn't have (the user may
    belong to several tenants, or none yet)."""

    sub: str  # user_id
    purpose: str
    jti: str
    exp: int
    iat: int
    fp: str | None = None


def create_purpose_token(
    *, user_id: uuid.UUID, purpose: str, expires_delta: timedelta, fingerprint: str | None = None
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "purpose": purpose,
        "jti": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
    }
    if fingerprint is not None:
        payload["fp"] = fingerprint
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_purpose_token(token: str, *, expected_purpose: str) -> uuid.UUID:
    return decode_purpose_token_full(token, expected_purpose=expected_purpose)[0]


def decode_purpose_token_full(
    token: str, *, expected_purpose: str
) -> tuple[uuid.UUID, str | None]:
    try:
        raw = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(str(exc)) from exc

    payload = PurposeTokenPayload.model_validate(raw)
    if payload.purpose != expected_purpose:
        raise InvalidTokenError(f"expected token purpose {expected_purpose}, got {payload.purpose}")
    return uuid.UUID(payload.sub), payload.fp
