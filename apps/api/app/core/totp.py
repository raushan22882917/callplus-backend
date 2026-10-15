"""RFC 6238 TOTP (HMAC-SHA1, 30s step, 6 digits by default) on the stdlib,
plus single-use recovery codes. No third-party dependency."""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP_SECONDS = 30
DIGITS = 6


def generate_secret(num_bytes: int = 20) -> str:
    """Base32 secret (no padding), the format authenticator apps expect."""
    return base64.b32encode(secrets.token_bytes(num_bytes)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    cleaned = secret.replace(" ", "").upper()
    return base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))


def hotp(key: bytes, counter: int, *, digits: int = DIGITS, algorithm: str = "sha1") -> str:
    digest = hmac.new(key, struct.pack(">Q", counter), getattr(hashlib, algorithm)).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return str(code).zfill(digits)


def totp_at(secret: str, for_time: float, *, digits: int = DIGITS, algorithm: str = "sha1") -> str:
    return hotp(_key(secret), int(for_time // STEP_SECONDS), digits=digits, algorithm=algorithm)


def verify_totp(
    secret: str,
    code: str,
    *,
    now: float | None = None,
    window: int = 1,
    min_step: int = 0,
) -> int | None:
    """Returns the matching time step (so the caller can store it and refuse
    replays) or None. Accepts +/- `window` steps of clock drift; steps
    <= min_step are rejected as already used."""
    code = code.strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    current = int((time.time() if now is None else now) // STEP_SECONDS)
    key = _key(secret)
    for step in range(current - window, current + window + 1):
        if step <= min_step:
            continue
        if hmac.compare_digest(hotp(key, step), code):
            return step
    return None


def provisioning_uri(secret: str, *, account: str, issuer: str) -> str:
    return (
        f"otpauth://totp/{quote(issuer)}:{quote(account)}"
        f"?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}"
        f"&period={STEP_SECONDS}"
    )


def generate_recovery_codes(count: int = 8) -> list[str]:
    return [
        f"{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}"
        for _ in range(count)
    ]


def hash_recovery_code(code: str) -> str:
    return hashlib.sha256(code.strip().lower().encode()).hexdigest()
