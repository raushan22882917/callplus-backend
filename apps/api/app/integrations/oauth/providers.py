"""Provider registry: real authorize/token/revoke endpoints, scopes and the
quirks of each provider (PKCE, offline access, data-centre awareness, per-shop
domains, token exchange shape). The platform's own client id/secret come from
settings (.env); everything tenant-specific is stored in `oauth_connections`.
"""

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from app.core.config import get_settings
from app.integrations.oauth.errors import OAuthError, ProviderNotConfigured

SHOPIFY_API_VERSION = "2024-10"
MS_CONSUMER_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"

_SHOP_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*\.myshopify\.com$")
_ZOHO_ACCOUNTS_RE = re.compile(r"^https://accounts\.zoho\.(com|eu|in|com\.au|jp|ca|sa|uk|cn)$")
_ZOHO_API_RE = re.compile(r"^https://www\.zohoapis\.(com|eu|in|com\.au|jp|ca|sa|uk|cn)$")
_SALESFORCE_HOST_RE = re.compile(r"^https://[a-z0-9.\-]+\.(salesforce\.com|force\.com)$")


def redirect_uri(provider: str) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/api/v1/oauth/{provider}/callback"


def login_redirect_uri(provider: str) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/api/v1/auth/oauth/{provider}/callback"


@dataclass
class TokenResult:
    access_token: str
    refresh_token: str | None = None
    expires_in: int | None = None
    scopes: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    # Provider data worth persisting in oauth_connections.metadata.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Identity:
    label: str
    email: str | None = None
    external_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _split_scopes(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if not value:
        return []
    return [s for s in re.split(r"[,\s]+", str(value)) if s]


def _body(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _error_text(response: httpx.Response) -> str:
    data = _body(response)
    err = data.get("error_description") or data.get("error") or data.get("message")
    if isinstance(err, dict):
        err = err.get("message")
    return str(err or response.text[:200] or f"HTTP {response.status_code}")


def _token_from_json(data: dict[str, Any]) -> TokenResult:
    token = data.get("access_token")
    if not token:
        raise OAuthError("token_exchange_failed", "The provider returned no access token")
    expires = data.get("expires_in")
    try:
        expires_in = int(expires) if expires is not None else None
    except (TypeError, ValueError):
        expires_in = None
    return TokenResult(
        access_token=token,
        refresh_token=data.get("refresh_token"),
        expires_in=expires_in,
        scopes=_split_scopes(data.get("scope")),
        raw=data,
    )


class Provider:
    key: str = ""
    label: str = ""
    category: str = ""
    description: str = ""
    # settings attribute names for the platform's own OAuth app
    id_attr: str = ""
    secret_attr: str = ""
    scopes: tuple[str, ...] = ()
    # Optional extra scope bundles a tenant can opt into at authorize time.
    scope_groups: dict[str, tuple[str, ...]] = {}
    scope_separator = " "
    pkce = False
    login = False  # usable for "Sign in with ..."
    requires_shop = False
    resources_kind: str | None = None  # calendars | channels
    features: tuple[str, ...] = ()
    authorize_url_base = ""
    token_url_base = ""
    revoke_supported = False
    # Meta has no refresh token: the long-lived access token is re-exchanged.
    refresh_from_access_token = False

    # -- credentials ---------------------------------------------------
    def client_id(self) -> str:
        return getattr(get_settings(), self.id_attr)

    def client_secret(self) -> str:
        return getattr(get_settings(), self.secret_attr)

    def configured(self) -> bool:
        return bool(self.client_id() and self.client_secret())

    def require_configured(self) -> None:
        if not self.configured():
            raise ProviderNotConfigured(self.key)

    # -- endpoints (overridable: depend on settings or per-connection data) --
    def authorize_url(self, shop: str | None = None) -> str:
        return self.authorize_url_base

    def token_url(self, extra: dict[str, Any] | None = None, shop: str | None = None) -> str:
        return self.token_url_base

    def resolve_scopes(self, options: list[str] | None = None) -> list[str]:
        scopes = list(self.scopes)
        for name in options or []:
            if name not in self.scope_groups:
                raise OAuthError("invalid_option", f"Unknown option {name!r} for {self.key}")
            scopes.extend(s for s in self.scope_groups[name] if s not in scopes)
        return scopes

    def authorize_params(
        self,
        *,
        state: str,
        scopes: list[str],
        code_challenge: str | None,
        redirect: str,
        shop: str | None = None,
    ) -> dict[str, str]:
        params = {
            "response_type": "code",
            "client_id": self.client_id(),
            "redirect_uri": redirect,
            "state": state,
            "scope": self.scope_separator.join(scopes),
        }
        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        params.update(self.extra_authorize_params())
        return params

    def extra_authorize_params(self) -> dict[str, str]:
        return {}

    def build_authorize_url(
        self,
        *,
        state: str,
        scopes: list[str],
        code_challenge: str | None,
        redirect: str | None = None,
        shop: str | None = None,
        for_login: bool = False,
    ) -> str:
        self.require_configured()
        params = self.authorize_params(
            state=state,
            scopes=scopes,
            code_challenge=code_challenge,
            redirect=redirect or redirect_uri(self.key),
            shop=shop,
        )
        if for_login:
            # Sign-in needs no offline access / forced consent screen.
            for key in ("access_type", "include_granted_scopes"):
                params.pop(key, None)
            if params.get("prompt") == "consent":
                params["prompt"] = "select_account"
        return f"{self.authorize_url(shop)}?{urlencode(params, quote_via=quote)}"

    # -- callback validation / token calls ------------------------------
    def validate_callback(self, params: dict[str, str], state_extra: dict[str, Any]) -> dict[str, Any]:
        """Provider-specific callback checks (Shopify HMAC, Zoho data centre).
        Returns extra data to carry into the token exchange."""
        return {}

    async def exchange_code(
        self,
        client: httpx.AsyncClient,
        *,
        code: str,
        code_verifier: str | None,
        redirect: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> TokenResult:
        self.require_configured()
        context = context or {}
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect or redirect_uri(self.key),
            "client_id": self.client_id(),
            "client_secret": self.client_secret(),
        }
        if code_verifier:
            data["code_verifier"] = code_verifier
        try:
            response = await client.post(
                self.token_url(context.get("connection_extra"), context.get("shop")),
                data=data,
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise OAuthError("token_exchange_failed", f"Token request failed: {exc}") from exc
        if response.status_code >= 400:
            raise OAuthError("token_exchange_failed", _error_text(response))
        return _token_from_json(_body(response))

    async def refresh(
        self, client: httpx.AsyncClient, *, refresh_token: str, extra: dict[str, Any]
    ) -> TokenResult:
        """Raises OAuthError(refresh_rejected) on 4xx grant errors and
        OAuthError(refresh_failed) for transient failures (5xx/network)."""
        self.require_configured()
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id(),
            "client_secret": self.client_secret(),
        }
        try:
            response = await client.post(
                self.token_url(extra), data=data, headers={"Accept": "application/json"}
            )
        except httpx.HTTPError as exc:
            raise OAuthError("refresh_failed", f"Token refresh failed: {exc}") from exc
        return self._parse_refresh(response, refresh_token)

    def _parse_refresh(self, response: httpx.Response, old_refresh: str | None) -> TokenResult:
        if response.status_code >= 500:
            raise OAuthError("refresh_failed", f"Provider error {response.status_code}")
        if response.status_code >= 400:
            raise OAuthError("refresh_rejected", _error_text(response))
        result = _token_from_json(_body(response))
        # Many providers do not re-issue the refresh token on refresh.
        if not result.refresh_token:
            result.refresh_token = old_refresh
        return result

    async def identity(
        self, client: httpx.AsyncClient, token: TokenResult, context: dict[str, Any]
    ) -> Identity:
        return Identity(label=self.label)

    async def revoke(
        self,
        client: httpx.AsyncClient,
        *,
        access_token: str,
        refresh_token: str | None,
        extra: dict[str, Any],
    ) -> None:
        """Best effort: callers swallow failures (the row is deleted anyway)."""
        return None


class GoogleProvider(Provider):
    key = "google"
    label = "Google"
    category = "Calendar & Sheets"
    description = "Sync appointments to Google Calendar, block busy times, optionally use Sheets."
    id_attr, secret_attr = "google_client_id", "google_client_secret"
    scopes = (
        "openid",
        "email",
        "profile",
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
        "https://www.googleapis.com/auth/calendar.freebusy",
    )
    scope_groups = {"sheets": ("https://www.googleapis.com/auth/spreadsheets",)}
    pkce = True
    login = True
    resources_kind = "calendars"
    features = ("calendar_sync", "busy_blocking")
    authorize_url_base = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url_base = "https://oauth2.googleapis.com/token"
    revoke_supported = True

    def extra_authorize_params(self) -> dict[str, str]:
        # offline + consent: guarantees a refresh token on every (re)connect.
        return {"access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"}

    async def identity(self, client, token, context) -> Identity:
        response = await client.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {token.access_token}"},
        )
        if response.status_code >= 400:
            raise OAuthError("identity_failed", _error_text(response))
        data = _body(response)
        email = data.get("email")
        return Identity(label=email or data.get("name") or "Google", email=email, external_id=data.get("sub"))

    async def revoke(self, client, *, access_token, refresh_token, extra) -> None:
        await client.post(
            "https://oauth2.googleapis.com/revoke", data={"token": refresh_token or access_token}
        )


class MicrosoftProvider(Provider):
    key = "microsoft"
    label = "Microsoft"
    category = "Calendar & Mail"
    description = "Sync appointments to Outlook Calendar and send mail through Microsoft Graph."
    id_attr, secret_attr = "microsoft_client_id", "microsoft_client_secret"
    scopes = (
        "openid",
        "email",
        "profile",
        "offline_access",
        "User.Read",
        "Calendars.ReadWrite",
        "Mail.Send",
        "Mail.Read",
    )
    pkce = True
    login = True
    resources_kind = "calendars"
    features = ("calendar_sync", "busy_blocking", "mail")

    def _tenant(self) -> str:
        return get_settings().microsoft_tenant_id or "common"

    def authorize_url(self, shop=None) -> str:
        return f"https://login.microsoftonline.com/{self._tenant()}/oauth2/v2.0/authorize"

    def token_url(self, extra=None, shop=None) -> str:
        return f"https://login.microsoftonline.com/{self._tenant()}/oauth2/v2.0/token"

    def extra_authorize_params(self) -> dict[str, str]:
        return {"response_mode": "query", "prompt": "select_account"}

    async def refresh(self, client, *, refresh_token, extra) -> TokenResult:
        # Microsoft rotates refresh tokens; the new one is persisted by the caller.
        return await super().refresh(client, refresh_token=refresh_token, extra=extra)

    async def identity(self, client, token, context) -> Identity:
        response = await client.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {token.access_token}"},
        )
        if response.status_code >= 400:
            raise OAuthError("identity_failed", _error_text(response))
        data = _body(response)
        email = data.get("mail") or data.get("userPrincipalName")
        return Identity(
            label=email or data.get("displayName") or "Microsoft",
            email=email,
            external_id=data.get("id"),
        )


class GmailProvider(GoogleProvider):
    """Same Google OAuth app as Calendar, separate connection so mail consent
    is not mixed into the calendar grant."""

    key = "gmail"
    label = "Gmail"
    category = "Email"
    description = "Send and receive email from a Gmail or Google Workspace mailbox."
    scopes = (
        "openid",
        "email",
        "profile",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
    )
    scope_groups = {}
    login = False
    resources_kind = None
    features = ("mail",)


class SalesforceProvider(Provider):
    key = "salesforce"
    label = "Salesforce"
    category = "CRM"
    description = "Create or update Salesforce Leads when a CRM lead is created."
    id_attr, secret_attr = "salesforce_client_id", "salesforce_client_secret"
    scopes = ("api", "refresh_token", "id")
    pkce = True
    features = ("crm_push",)
    revoke_supported = True

    def _login_url(self) -> str:
        return (get_settings().salesforce_login_url or "https://login.salesforce.com").rstrip("/")

    def authorize_url(self, shop=None) -> str:
        return f"{self._login_url()}/services/oauth2/authorize"

    def token_url(self, extra=None, shop=None) -> str:
        return f"{self._login_url()}/services/oauth2/token"

    async def exchange_code(self, client, *, code, code_verifier, redirect=None, context=None):
        token = await super().exchange_code(
            client, code=code, code_verifier=code_verifier, redirect=redirect, context=context
        )
        instance = token.raw.get("instance_url", "")
        if not _SALESFORCE_HOST_RE.match(instance):
            raise OAuthError("token_exchange_failed", "Salesforce returned an unexpected instance URL")
        token.extra["instance_url"] = instance
        token.extra["identity_url"] = token.raw.get("id")
        return token

    async def identity(self, client, token, context) -> Identity:
        url = token.extra.get("identity_url") or ""
        if "/id/" not in url or not _SALESFORCE_HOST_RE.match(url.split("/id/")[0]):
            return Identity(label="Salesforce")
        response = await client.get(url, headers={"Authorization": f"Bearer {token.access_token}"})
        if response.status_code >= 400:
            raise OAuthError("identity_failed", _error_text(response))
        data = _body(response)
        return Identity(
            label=data.get("email") or data.get("display_name") or "Salesforce",
            email=data.get("email"),
            external_id=data.get("user_id"),
            extra={"organization_id": data.get("organization_id")},
        )

    def _parse_refresh(self, response, old_refresh) -> TokenResult:
        result = super()._parse_refresh(response, old_refresh)
        if result.raw.get("instance_url"):
            result.extra["instance_url"] = result.raw["instance_url"]
        return result

    async def revoke(self, client, *, access_token, refresh_token, extra) -> None:
        await client.post(
            f"{self._login_url()}/services/oauth2/revoke", data={"token": refresh_token or access_token}
        )


class HubSpotProvider(Provider):
    key = "hubspot"
    label = "HubSpot"
    category = "CRM"
    description = "Upsert HubSpot contacts when a CRM lead is created."
    id_attr, secret_attr = "hubspot_client_id", "hubspot_client_secret"
    scopes = (
        "oauth",
        "crm.objects.contacts.read",
        "crm.objects.contacts.write",
        "crm.objects.deals.read",
        "crm.objects.deals.write",
    )
    features = ("crm_push",)
    authorize_url_base = "https://app.hubspot.com/oauth/authorize"
    token_url_base = "https://api.hubapi.com/oauth/v1/token"
    revoke_supported = True

    async def identity(self, client, token, context) -> Identity:
        response = await client.get(
            f"https://api.hubapi.com/oauth/v1/access-tokens/{quote(token.access_token, safe='')}"
        )
        if response.status_code >= 400:
            raise OAuthError("identity_failed", _error_text(response))
        data = _body(response)
        return Identity(
            label=data.get("hub_domain") or data.get("user") or "HubSpot",
            email=data.get("user"),
            external_id=str(data.get("hub_id")) if data.get("hub_id") is not None else None,
        )

    async def revoke(self, client, *, access_token, refresh_token, extra) -> None:
        if refresh_token:
            await client.delete(
                f"https://api.hubapi.com/oauth/v1/refresh-tokens/{quote(refresh_token, safe='')}"
            )


class ZohoProvider(Provider):
    key = "zoho"
    label = "Zoho CRM"
    category = "CRM"
    description = "Upsert Zoho CRM leads when a CRM lead is created."
    id_attr, secret_attr = "zoho_client_id", "zoho_client_secret"
    scopes = ("ZohoCRM.modules.ALL", "ZohoCRM.users.READ", "AaaServer.profile.READ")
    scope_separator = ","
    features = ("crm_push",)
    revoke_supported = True

    def _accounts(self, extra: dict[str, Any] | None = None) -> str:
        domain = (
            (extra or {}).get("accounts_server")
            or get_settings().zoho_accounts_domain
            or "https://accounts.zoho.com"  # an empty ZOHO_ACCOUNTS_DOMAIN placeholder = default
        ).rstrip("/")
        if not _ZOHO_ACCOUNTS_RE.match(domain):
            raise OAuthError("invalid_zoho_domain", "Zoho accounts domain must be an official accounts.zoho.* host")
        return domain

    def authorize_url(self, shop=None) -> str:
        return f"{self._accounts()}/oauth/v2/auth"

    def token_url(self, extra=None, shop=None) -> str:
        return f"{self._accounts(extra)}/oauth/v2/token"

    def extra_authorize_params(self) -> dict[str, str]:
        return {"access_type": "offline", "prompt": "consent"}

    def validate_callback(self, params, state_extra) -> dict[str, Any]:
        # Zoho tells us the user's own data centre; the code is only valid there.
        server = params.get("accounts-server")
        if server:
            if not _ZOHO_ACCOUNTS_RE.match(server.rstrip("/")):
                raise OAuthError("invalid_zoho_domain", "Unexpected Zoho accounts server")
            return {"connection_extra": {"accounts_server": server.rstrip("/")}}
        return {}

    async def exchange_code(self, client, *, code, code_verifier, redirect=None, context=None):
        token = await super().exchange_code(
            client, code=code, code_verifier=code_verifier, redirect=redirect, context=context
        )
        accounts = ((context or {}).get("connection_extra") or {}).get("accounts_server") or self._accounts()
        token.extra["accounts_server"] = accounts
        api_domain = (token.raw.get("api_domain") or "").rstrip("/")
        if api_domain and _ZOHO_API_RE.match(api_domain):
            token.extra["api_domain"] = api_domain
        return token

    async def identity(self, client, token, context) -> Identity:
        accounts = token.extra.get("accounts_server") or self._accounts()
        response = await client.get(
            f"{accounts}/oauth/user/info",
            headers={"Authorization": f"Zoho-oauthtoken {token.access_token}"},
        )
        if response.status_code >= 400:
            return Identity(label="Zoho CRM")
        data = _body(response)
        email = data.get("Email")
        return Identity(
            label=email or data.get("Display_Name") or "Zoho CRM",
            email=email,
            external_id=str(data.get("ZUID")) if data.get("ZUID") else None,
        )

    async def revoke(self, client, *, access_token, refresh_token, extra) -> None:
        await client.post(
            f"{self._accounts(extra)}/oauth/v2/token/revoke",
            params={"token": refresh_token or access_token},
        )


class SlackProvider(Provider):
    key = "slack"
    label = "Slack"
    category = "Notifications"
    description = "Post new-ticket and SLA-breach alerts to a Slack channel."
    id_attr, secret_attr = "slack_client_id", "slack_client_secret"
    scopes = ("chat:write", "chat:write.public", "channels:read", "groups:read")
    scope_separator = ","
    resources_kind = "channels"
    features = ("notifications",)
    authorize_url_base = "https://slack.com/oauth/v2/authorize"
    token_url_base = "https://slack.com/api/oauth.v2.access"
    revoke_supported = True

    def authorize_params(self, *, state, scopes, code_challenge, redirect, shop=None):
        # Slack v2: `scope` carries the BOT scopes; no response_type parameter.
        return {
            "client_id": self.client_id(),
            "scope": ",".join(scopes),
            "redirect_uri": redirect,
            "state": state,
        }

    @staticmethod
    def _unwrap(response: httpx.Response, code: str) -> dict[str, Any]:
        if response.status_code >= 500:
            raise OAuthError("refresh_failed" if code == "refresh" else "token_exchange_failed", f"Slack error {response.status_code}")
        data = _body(response)
        if not data.get("ok"):
            kind = "refresh_rejected" if code == "refresh" else "token_exchange_failed"
            raise OAuthError(kind, str(data.get("error") or "slack_error"))
        return data

    async def exchange_code(self, client, *, code, code_verifier, redirect=None, context=None):
        self.require_configured()
        try:
            response = await client.post(
                self.token_url_base,
                data={
                    "code": code,
                    "redirect_uri": redirect or redirect_uri(self.key),
                    "client_id": self.client_id(),
                    "client_secret": self.client_secret(),
                },
            )
        except httpx.HTTPError as exc:
            raise OAuthError("token_exchange_failed", f"Token request failed: {exc}") from exc
        data = self._unwrap(response, "exchange")
        token = _token_from_json(data)  # the bot token (top-level access_token)
        team = data.get("team") or {}
        token.extra.update(
            team_id=team.get("id"),
            team_name=team.get("name"),
            bot_user_id=data.get("bot_user_id"),
            authed_user_id=(data.get("authed_user") or {}).get("id"),
        )
        return token

    async def refresh(self, client, *, refresh_token, extra) -> TokenResult:
        try:
            response = await client.post(
                self.token_url_base,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self.client_id(),
                    "client_secret": self.client_secret(),
                },
            )
        except httpx.HTTPError as exc:
            raise OAuthError("refresh_failed", f"Token refresh failed: {exc}") from exc
        result = _token_from_json(self._unwrap(response, "refresh"))
        result.refresh_token = result.refresh_token or refresh_token
        return result

    async def identity(self, client, token, context) -> Identity:
        return Identity(
            label=token.extra.get("team_name") or "Slack workspace",
            external_id=token.extra.get("team_id"),
            extra={"team_name": token.extra.get("team_name"), "bot_user_id": token.extra.get("bot_user_id")},
        )

    async def revoke(self, client, *, access_token, refresh_token, extra) -> None:
        await client.post(
            "https://slack.com/api/auth.revoke", headers={"Authorization": f"Bearer {access_token}"}
        )


class ShopifyProvider(Provider):
    key = "shopify"
    label = "Shopify"
    category = "Commerce"
    description = "Let AI agents look up Shopify order status (lookup_order tool)."
    id_attr, secret_attr = "shopify_client_id", "shopify_client_secret"
    scopes = ("read_orders", "read_customers", "read_products")
    scope_separator = ","
    requires_shop = True
    features = ("orders",)
    revoke_supported = True

    @staticmethod
    def normalize_shop(shop: str | None) -> str:
        value = (shop or "").strip().lower()
        value = re.sub(r"^https?://", "", value).split("/")[0]
        if value and "." not in value:
            value = f"{value}.myshopify.com"
        if not _SHOP_RE.match(value):
            raise OAuthError("invalid_shop", "Enter your store domain, e.g. my-store.myshopify.com")
        return value

    def authorize_url(self, shop=None) -> str:
        return f"https://{self.normalize_shop(shop)}/admin/oauth/authorize"

    def token_url(self, extra=None, shop=None) -> str:
        return f"https://{self.normalize_shop(shop)}/admin/oauth/access_token"

    def authorize_params(self, *, state, scopes, code_challenge, redirect, shop=None):
        return {
            "client_id": self.client_id(),
            "scope": ",".join(scopes),
            "redirect_uri": redirect,
            "state": state,
        }

    @staticmethod
    def hmac_valid(params: dict[str, str], secret: str) -> bool:
        """Shopify signs the callback query: HMAC-SHA256 hex over the sorted
        `key=value` pairs (excluding `hmac`) joined by '&'."""
        supplied = params.get("hmac", "")
        message = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k not in ("hmac", "signature"))
        digest = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        return bool(supplied) and hmac.compare_digest(digest, supplied)

    def validate_callback(self, params, state_extra) -> dict[str, Any]:
        self.require_configured()
        if not self.hmac_valid(params, self.client_secret()):
            raise OAuthError("invalid_hmac", "Shopify callback signature is invalid")
        shop = self.normalize_shop(params.get("shop"))
        if shop != state_extra.get("shop"):
            raise OAuthError("invalid_state", "Shop does not match the authorization request")
        return {"shop": shop}

    async def exchange_code(self, client, *, code, code_verifier, redirect=None, context=None):
        self.require_configured()
        shop = (context or {}).get("shop")
        try:
            response = await client.post(
                self.token_url(shop=shop),
                json={
                    "client_id": self.client_id(),
                    "client_secret": self.client_secret(),
                    "code": code,
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise OAuthError("token_exchange_failed", f"Token request failed: {exc}") from exc
        if response.status_code >= 400:
            raise OAuthError("token_exchange_failed", _error_text(response))
        token = _token_from_json(_body(response))  # offline token: no expiry/refresh
        token.extra["shop"] = shop
        return token

    async def identity(self, client, token, context) -> Identity:
        shop = token.extra.get("shop") or context.get("shop")
        label, email = shop, None
        try:
            response = await client.get(
                f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/shop.json",
                headers={"X-Shopify-Access-Token": token.access_token},
            )
            if response.status_code < 400:
                info = _body(response).get("shop", {})
                label = info.get("name") or shop
                email = info.get("email")
        except httpx.HTTPError:
            pass
        return Identity(label=f"{label} ({shop})" if label != shop else shop, email=email, external_id=shop, extra={"shop": shop})

    async def revoke(self, client, *, access_token, refresh_token, extra) -> None:
        shop = extra.get("shop")
        if shop:
            await client.delete(
                f"https://{shop}/admin/api_permissions/current.json",
                headers={"X-Shopify-Access-Token": access_token},
            )


PROVIDERS: dict[str, Provider] = {
    p.key: p
    for p in (
        GoogleProvider(),
        GmailProvider(),
        MicrosoftProvider(),
        SalesforceProvider(),
        HubSpotProvider(),
        ZohoProvider(),
        SlackProvider(),
        ShopifyProvider(),
    )
}


def get_provider(key: str) -> Provider:
    provider = PROVIDERS.get(key)
    if provider is None:
        raise OAuthError("unknown_provider", f"Unknown OAuth provider {key!r}", status_code=404)
    return provider
