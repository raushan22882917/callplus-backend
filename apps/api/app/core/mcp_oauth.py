"""Helpers for the OAuth 2.1 authorization server in front of /mcp: token
generation/hashing, PKCE, redirect-URI and scope rules (RFC 6749, 7636, 7591,
8707; the MCP authorization spec)."""

import base64
import hashlib
import hmac
import secrets
from urllib.parse import urlparse

from app.core.api_keys import SCOPE_PERMISSIONS, hash_api_key
from app.core.config import get_settings

ACCESS_PREFIX = "ccb_oat_"
REFRESH_PREFIX = "ccb_ort_"
ACCESS_TOKEN_SECONDS = 3600
REFRESH_TOKEN_DAYS = 30
CODE_SECONDS = 600

# Webhook management is a REST-API capability with no MCP tools, so it is
# never offered through OAuth.
OAUTH_SCOPES: tuple[str, ...] = tuple(sorted(set(SCOPE_PERMISSIONS) - {"webhooks:manage"}))
DEFAULT_SCOPES: tuple[str, ...] = tuple(s for s in OAUTH_SCOPES if s.endswith(":read"))

_BLOCKED_SCHEMES = {"javascript", "data", "file", "vbscript", "about", "blob", "ftp"}
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def issuer() -> str:
    return get_settings().public_base_url.rstrip("/")


def resource_url() -> str:
    return f"{issuer()}/mcp"


def same_resource(value: str | None) -> bool:
    return value is None or value.rstrip("/") == resource_url()


def new_secret(prefix: str) -> tuple[str, str]:
    """(plaintext, sha256 hash). Only the hash is stored."""
    plaintext = f"{prefix}{secrets.token_urlsafe(32)}"
    return plaintext, hash_api_key(plaintext)


def hash_token(value: str) -> str:
    return hash_api_key(value)


def pkce_matches(verifier: str, challenge: str) -> bool:
    if not (43 <= len(verifier) <= 128):
        return False
    digest = hashlib.sha256(verifier.encode("ascii", "ignore")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(expected, challenge)


def valid_redirect_uri(uri: str) -> bool:
    """https, loopback http (native apps, RFC 8252), or a private-use custom
    scheme. No fragments, no credentials, no script-capable schemes."""
    if not isinstance(uri, str) or len(uri) > 2048:
        return False
    try:
        parsed = urlparse(uri)
    except ValueError:
        return False
    scheme = parsed.scheme.lower()
    if not scheme or scheme in _BLOCKED_SCHEMES or parsed.fragment or parsed.username:
        return False
    if scheme == "https":
        return bool(parsed.hostname)
    if scheme == "http":
        return parsed.hostname in _LOOPBACK
    return True  # private-use scheme for a native app, e.g. cursor:// (RFC 8252 7.1)


def redirect_matches(registered: list[str], requested: str) -> bool:
    """Exact match, except loopback redirects may vary their port (RFC 8252 7.3)."""
    if requested in registered:
        return True
    req = urlparse(requested)
    if req.scheme != "http" or req.hostname not in _LOOPBACK:
        return False
    for uri in registered:
        reg = urlparse(uri)
        if (reg.scheme, reg.hostname, reg.path, reg.query) == (
            req.scheme,
            req.hostname,
            req.path,
            req.query,
        ):
            return True
    return False
