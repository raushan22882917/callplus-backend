from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Values that ship in the repo / .env.example and must never guard a real deployment.
_INSECURE_JWT = {"change-me-in-production", "changeme", "secret"}
_PUBLIC_ENCRYPTION_KEY = "hF1c3zL1Yv8m3q9y6t2u5b7n0k4j8h1s2d5f8g1h4j7="
_LOCAL_ENVS = {"development", "dev", "local", "test", "testing"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "development"
    debug: bool = False

    database_url: str = "postgresql+asyncpg://callcenter:callcenter@localhost:5432/callcenter"
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret_key: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    # Signs Exotel status callbacks. Separate from the login key so a leaked
    # callback secret cannot mint session tokens. Local and test fall back to
    # the JWT secret when this is empty; production must set its own.
    exotel_webhook_secret: str = ""
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 14

    cors_allow_origins: list[str] = ["http://localhost:3000"]

    # Fernet key (44-char urlsafe-base64) for encrypting integration secrets
    # at rest (WhatsApp access tokens, etc). Dev-only default — generate a
    # real one with `python -c "from cryptography.fernet import Fernet;
    # print(Fernet.generate_key().decode())"` for any non-local environment.
    encryption_key: str = "hF1c3zL1Yv8m3q9y6t2u5b7n0k4j8h1s2d5f8g1h4j7="

    # Shared secret the Go realtime gateway sends on internal/* endpoints.
    # These endpoints derive tenant_id from the call row looked up by
    # call_id — this token only proves "the caller is our own realtime
    # plane", it does not itself grant a tenant scope.
    internal_service_token: str = "change-me-in-production"

    # Public wss:// URL of the Go realtime gateway's Twilio Media Stream
    # endpoint, used when building the TwiML for inbound/outbound calls.
    realtime_ws_url: str = "wss://localhost:9000/ws/twilio"

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    # Public https:// origin of this FastAPI service, exactly as configured
    # in Twilio (console webhook URL / the Url we pass when placing outbound
    # calls) — required for Twilio request-signature validation, which
    # signs the exact URL Twilio was told to hit.
    public_base_url: str = "http://localhost:8000"
    # Public key of a web chat channel (with voice calls on) that the marketing site's "Talk to the AI" button
    # calls. Empty hides the button.
    web_call_demo_key: str = ""
    # Where a transfer_call tool invocation redirects the live call to. If
    # unset, a transfer just speaks the handoff message and hangs up rather
    # than dialing anywhere — there's no human queue configured by default.
    transfer_fallback_number: str = ""

    # Minimum seconds between consecutive campaign sends, per channel, so a
    # large broadcast stays under Twilio's calls-per-second and Meta's
    # messaging throughput limits. Set to 0 to disable (tests do).
    campaign_voice_interval_seconds: float = 1.0
    campaign_whatsapp_interval_seconds: float = 0.2
    campaign_email_interval_seconds: float = 0.3
    campaign_sms_interval_seconds: float = 0.2
    # Transient provider failures (Twilio/Meta errors) are retried this many
    # times in total per contact, waiting backoff * attempt seconds between
    # tries. Config errors (no phone number, no template) are never retried.
    campaign_max_send_attempts: int = 3
    campaign_retry_backoff_seconds: float = 2.0

    sarvam_api_key: str = ""
    # Optional real embeddings. Empty keeps the built-in word hasher. The API must return 384 numbers
    # per text (OpenAI-compatible, with a dimensions field). A wrong size is refused, not stored.
    embeddings_api_url: str = ""
    embeddings_api_key: str = ""
    embeddings_model: str = "text-embedding-3-small"
    # ---- "CallPlus" (managed) providers, offered next to a workspace's own account.
    # Sarvam is offered whenever SARVAM_API_KEY is set (included minutes). Deepgram, ElevenLabs
    # and a separate language model are offered only when listed below and their key is set.
    deepgram_api_key: str = ""
    elevenlabs_api_key: str = ""
    elevenlabs_model: str = "eleven_flash_v2_5"
    managed_llm_api_key: str = ""
    managed_llm_url: str = ""  # an OpenAI-compatible chat-completions address; empty = use Sarvam's model
    managed_llm_model: str = ""
    managed_llm_label: str = "CallPlus AI"
    # Comma list among: deepgram, elevenlabs, llm. Sarvam does not need to be listed.
    managed_providers: str = ""
    # What a minute costs us more than a minute on Sarvam, as extra minutes counted against the workspace's plan
    # or wallet when a call uses that managed provider ("elevenlabs:2" = a call counts three minutes per minute).
    # Starting points: check them against your providers' invoices.
    managed_minute_extras: str = "deepgram:0.2,elevenlabs:1.0,llm:0"
    # How many cloned voices one workspace may keep. A clone takes a voice slot on the account it is made on, and
    # slots are limited by that account's plan, so CallPlus's own account is rationed more tightly.
    voice_clones_managed_cap: int = 2
    voice_clones_own_cap: int = 25
    # Text channels (web chat, WhatsApp, SMS, email) answer with Sarvam's fast
    # conversational model, ~0.4s per reply instead of 8-10s on the reasoning model. Set to false to
    # roll back to the reasoning model for text.
    fast_model_for_text: bool = True

    # Plan a brand-new workspace starts on (see app/services/plans.py). 'free' is
    # the 10-minute trial; 'legacy' means unlimited (what existing tenants have).
    signup_plan_tier: str = "free"

    # Public API (API-key authenticated) guard rails.
    api_key_rate_limit_per_minute: int = 120
    max_concurrent_calls_per_tenant: int = 10
    # Indicative per-unit prices (paise) behind the Usage tab's cost estimate.
    # Set these to your real wholesale+margin rates; they are not billing.
    usage_paise_per_call_minute: int = 600
    usage_paise_per_whatsapp_message: int = 50
    # Indicative all-in cost of one minute of AI-handled call (telephony + STT/TTS + LLM),
    # used by /insights/ai-analytics. Not billing; set to your real blended rate.
    ai_call_cost_paise_per_minute: int = 400

    meta_whatsapp_app_secret: str = ""
    meta_whatsapp_verify_token: str = "change-me-in-production"
    meta_graph_api_version: str = "v21.0"

    # ------------------------------------------------------------------
    # Per-tenant OAuth: the PLATFORM's own OAuth app per provider. Each
    # business then clicks "Connect" and authorizes; that tenant's tokens
    # are stored encrypted in `oauth_connections` (never in .env). All
    # redirect URIs below use PUBLIC_BASE_URL, e.g.
    #   {PUBLIC_BASE_URL}/api/v1/oauth/<provider>/callback
    # (see docs/OAUTH_SETUP.md for a console-by-console checklist).
    # ------------------------------------------------------------------

    # Google (console.cloud.google.com > APIs & Services > Credentials, OAuth client "Web").
    # Redirect URIs to register:
    #   {PUBLIC_BASE_URL}/api/v1/oauth/google/callback        (connect Calendar/Sheets)
    #   {PUBLIC_BASE_URL}/api/v1/auth/oauth/google/callback   (Sign in with Google)
    # Scopes: openid email profile, calendar.events, calendar.calendarlist.readonly,
    #   calendar.freebusy; optional group "sheets" adds spreadsheets. access_type=offline.
    google_client_id: str = ""
    google_client_secret: str = ""

    # Microsoft Entra (portal.azure.com > App registrations; supported account types:
    # "any org directory + personal accounts", so the tenant is `common`).
    # Redirect URIs (platform type "Web"):
    #   {PUBLIC_BASE_URL}/api/v1/oauth/microsoft/callback
    #   {PUBLIC_BASE_URL}/api/v1/auth/oauth/microsoft/callback   (Sign in with Microsoft)
    # Delegated scopes: openid email profile offline_access User.Read Calendars.ReadWrite Mail.Send
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""
    microsoft_tenant_id: str = "common"

    # Salesforce (Setup > App Manager > New Connected App, enable OAuth).
    # Redirect URI: {PUBLIC_BASE_URL}/api/v1/oauth/salesforce/callback
    # Scopes: api, refresh_token (offline_access), id. PKCE is used.
    # Use SALESFORCE_LOGIN_URL=https://test.salesforce.com for sandboxes.
    salesforce_client_id: str = ""
    salesforce_client_secret: str = ""
    salesforce_login_url: str = "https://login.salesforce.com"

    # HubSpot (developers.hubspot.com > Apps > Auth tab).
    # Redirect URI: {PUBLIC_BASE_URL}/api/v1/oauth/hubspot/callback
    # Scopes: oauth, crm.objects.contacts.read/write, crm.objects.deals.read/write
    hubspot_client_id: str = ""
    hubspot_client_secret: str = ""

    # Zoho (api-console.zoho.in or .com > Server-based Applications).
    # Redirect URI: {PUBLIC_BASE_URL}/api/v1/oauth/zoho/callback
    # Scopes: ZohoCRM.modules.ALL, ZohoCRM.users.READ, AaaServer.profile.READ (offline access).
    # ZOHO_ACCOUNTS_DOMAIN is the data centre where the client was registered
    # (https://accounts.zoho.in, .com, .eu, .com.au, .jp ...). Zoho tells us the
    # user's own data centre on the callback, which is honoured automatically.
    zoho_client_id: str = ""
    zoho_client_secret: str = ""
    zoho_accounts_domain: str = "https://accounts.zoho.com"

    # Slack (api.slack.com/apps > OAuth & Permissions).
    # Redirect URI: {PUBLIC_BASE_URL}/api/v1/oauth/slack/callback
    # Bot scopes: chat:write, chat:write.public, channels:read, groups:read
    slack_client_id: str = ""
    slack_client_secret: str = ""

    # Meta app used by WhatsApp Cloud API and Embedded Signup.
    # META_APP_SECRET falls back to META_WHATSAPP_APP_SECRET (same Meta app).
    # META_WHATSAPP_CONFIG_ID: Embedded Signup configuration id.
    meta_app_id: str = ""
    meta_app_secret: str = ""
    meta_login_config_id: str = ""
    meta_whatsapp_config_id: str = ""

    # Shopify (partners.shopify.com > Apps > your app > Configuration).
    # Allowed redirection URL: {PUBLIC_BASE_URL}/api/v1/oauth/shopify/callback
    # Scopes: read_orders, read_customers, read_products. Per-shop domain flow with
    # HMAC verification of the callback.
    shopify_client_id: str = ""
    shopify_client_secret: str = ""

    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    # PayPal Checkout for wallet top-ups (developer.paypal.com > Apps & Credentials).
    # Webhook URL: {PUBLIC_BASE_URL}/api/v1/webhooks/payment/paypal
    # Events: CHECKOUT.ORDER.APPROVED, PAYMENT.CAPTURE.COMPLETED.
    # paypal_mode is "sandbox" or "live". The client id is public; the secret is not.
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_webhook_id: str = ""
    paypal_mode: str = "sandbox"
    # Fallback when the live USD to INR rate cannot be read. PayPal collects dollars.
    # The wallet still credits rupees. The charged order stores the rate it actually used.
    paypal_inr_per_usd: int = 88
    # Who the GST invoice for a wallet top-up is issued by. The state code (GST numeric,
    # e.g. "08" Rajasthan) decides CGST+SGST (same state) versus IGST (another state).
    seller_legal_name: str = "CallPlus"
    seller_gstin: str = ""
    # Taken off the price when all three channel packs are bought in one payment (0 to 50).
    pack_bundle_discount_percent: int = 10
    seller_state_code: str = ""
    seller_address: str = ""

    # SMTP for transactional email (password reset, email verification). If
    # smtp_host is unset (the dev default), app.integrations.email_client
    # logs the email instead of sending it rather than failing outright.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = "no-reply@example.com"
    smtp_use_tls: bool = True
    # Email over HTTPS (port 443), for hosts that block SMTP. "resend" or "brevo".
    # When email_api_key is set it is used instead of SMTP.
    email_api_provider: str = "resend"
    email_api_key: str = ""
    # Delivery events (bounce, spam complaint) from the email provider. Both fail closed when unset.
    # Resend signs with Svix: paste the "whsec_..." signing secret. Brevo cannot sign, so it sends
    # this token in an "Authorization: Bearer <token>" header you add to the webhook.
    email_resend_webhook_secret: str = ""
    email_brevo_webhook_token: str = ""

    # Base URL of the tenant-facing frontend, used to build links embedded
    # in transactional emails (password reset, email verification).
    frontend_base_url: str = "http://localhost:3000"

    # Honour X-Forwarded-For for the per-tenant IP allowlist. Only enable
    # behind a reverse proxy that overwrites the header (it is spoofable
    # otherwise).
    trust_x_forwarded_for: bool = False

    # Per-IP throttle on login / signup / password-reset (requests per minute).
    auth_rate_limit_enabled: bool = True
    auth_login_rate_limit_per_minute: int = 10
    auth_signup_rate_limit_per_minute: int = 5
    auth_reset_rate_limit_per_minute: int = 5

    # Serve /docs, /redoc and /openapi.json. Off by default outside local envs.
    enable_api_docs: bool | None = None


    @property
    def docs_enabled(self) -> bool:
        return self.is_local if self.enable_api_docs is None else self.enable_api_docs

    @property
    def is_local(self) -> bool:
        return self.env.lower() in _LOCAL_ENVS

    @model_validator(mode="after")
    def _reject_insecure_production_config(self) -> "Settings":
        """Fail fast at boot: outside local/test environments the app refuses to
        start with placeholder secrets, debug on, or an open CORS policy."""
        if self.is_local:
            return self
        problems: list[str] = []
        if self.jwt_secret_key in _INSECURE_JWT or len(self.jwt_secret_key) < 32:
            problems.append("JWT_SECRET_KEY must be a random value of at least 32 characters")
        if self.exotel_webhook_secret in _INSECURE_JWT or len(self.exotel_webhook_secret) < 24:
            problems.append(
                "EXOTEL_WEBHOOK_SECRET must be a random value of at least 24 characters, "
                "different from JWT_SECRET_KEY"
            )
        if self.exotel_webhook_secret and self.exotel_webhook_secret == self.jwt_secret_key:
            problems.append("EXOTEL_WEBHOOK_SECRET must not be the same value as JWT_SECRET_KEY")
        if self.encryption_key == _PUBLIC_ENCRYPTION_KEY:
            problems.append("ENCRYPTION_KEY is the public dev key; generate a real Fernet key")
        if self.internal_service_token in _INSECURE_JWT or len(self.internal_service_token) < 24:
            problems.append("INTERNAL_SERVICE_TOKEN must be random and at least 24 characters")
        if self.meta_whatsapp_verify_token in _INSECURE_JWT:
            problems.append("META_WHATSAPP_VERIFY_TOKEN must not be the placeholder value")
        if self.debug:
            problems.append("DEBUG must be false")
        if "*" in self.cors_allow_origins:
            problems.append("CORS_ALLOW_ORIGINS must not contain '*' (credentials are allowed)")
        if problems:
            raise ValueError(f"Insecure configuration for ENV={self.env}: " + "; ".join(problems))
        return self

    @property
    def meta_oauth_secret(self) -> str:
        """Meta app secret for OAuth/Graph calls (META_APP_SECRET, else the WhatsApp one)."""
        return self.meta_app_secret or self.meta_whatsapp_app_secret

    @property
    def meta_webhook_secret(self) -> str:
        """Secret that signs Meta webhooks (same Meta app, so either variable works)."""
        return self.meta_whatsapp_app_secret or self.meta_app_secret


@lru_cache
def get_settings() -> Settings:
    return Settings()
