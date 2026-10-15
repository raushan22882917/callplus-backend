"""Generic per-tenant OAuth framework (see docs/OAUTH_SETUP.md)."""

from app.integrations.oauth.errors import OAuthError, ProviderNotConfigured
from app.integrations.oauth.tokens import get_valid_access_token

__all__ = ["OAuthError", "ProviderNotConfigured", "get_valid_access_token"]
