"""Generates and hashes MCP API keys (see packages/database/models/mcp.py).

These are opaque, high-entropy bearer tokens rather than passwords a human
picks, so a fast SHA-256 hash is the right tool here — unlike
app.core.security.hash_password, there's no low-entropy input to slow down
an attacker's guessing against.
"""

import hashlib
import secrets

_PREFIX = "ccb_mcp_"


def generate_api_key() -> tuple[str, str, str]:
    """Returns (plaintext_key, display_prefix, hashed_key). The plaintext
    key is only ever returned here — it is never stored, so this is the
    caller's one chance to hand it to the user."""
    plaintext_key = f"{_PREFIX}{secrets.token_urlsafe(32)}"
    display_prefix = plaintext_key[: len(_PREFIX) + 8]
    return plaintext_key, display_prefix, hash_api_key(plaintext_key)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# --- Public API keys (packages/database/models/developer.py: ApiKey) -------

API_KEY_PREFIXES = ("ak_live_", "ak_test_")

# What each scope lets a key do, expressed as the permission codes the rest of
# the API already enforces — so a key can never reach anything a human role
# with those same codes couldn't (no billing, users, integrations, ...).
SCOPE_PERMISSIONS: dict[str, frozenset[str]] = {
    "calls:read": frozenset({"calls.read"}),
    "calls:write": frozenset({"calls.read", "calls.manage"}),
    "agents:read": frozenset({"agents.read"}),
    "agents:write": frozenset({"agents.read", "agents.manage"}),
    "customers:read": frozenset({"customers.read"}),
    "customers:write": frozenset({"customers.read", "customers.manage"}),
    "messages:write": frozenset({"conversations.read", "conversations.manage"}),
    # Numbers map onto the agents.* permissions the phone-number routes already
    # enforce, so numbers:write also lets the key reassign/edit AI agents.
    "numbers:read": frozenset({"agents.read"}),
    "numbers:write": frozenset({"agents.read", "agents.manage"}),
    "campaigns:read": frozenset({"campaigns.read"}),
    "campaigns:write": frozenset({"campaigns.read", "campaigns.manage"}),
    # Manages webhook endpoints via /developer/webhooks; grants no other permission.
    "webhooks:manage": frozenset(),
}


def permissions_for_scopes(scopes: list[str] | tuple[str, ...]) -> frozenset[str]:
    codes: set[str] = set()
    for scope in scopes:
        codes |= SCOPE_PERMISSIONS.get(scope, frozenset())
    return frozenset(codes)


def generate_developer_key(mode: str) -> tuple[str, str, str]:
    """Returns (plaintext_key, display_prefix, hashed_key) for an `ak_live_` or
    `ak_test_` key. Plaintext is returned once and never stored."""
    prefix = "ak_live_" if mode == "live" else "ak_test_"
    plaintext_key = f"{prefix}{secrets.token_urlsafe(32)}"
    return plaintext_key, plaintext_key[: len(prefix) + 6], hash_api_key(plaintext_key)
