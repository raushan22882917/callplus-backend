# OAuth setup (per-tenant connections)

AutoNXT is multi-tenant. **Only the platform's own OAuth app** (client id and
secret, one per provider) lives in `.env`. Each business clicks **Connect** in
*Integrations > Hub*, authorizes, and that tenant's tokens are stored encrypted
(Fernet, `ENCRYPTION_KEY`) in the `oauth_connections` table and refreshed
automatically.

Set `PUBLIC_BASE_URL` (the public https origin of this API) and
`FRONTEND_BASE_URL` first. Redirect URI pattern for every provider:

    {PUBLIC_BASE_URL}/api/v1/oauth/<provider>/callback

Sign in with Google uses a second, separate redirect URI (Google is the only sign-in provider; Microsoft is not
offered for sign-in, and the product no longer offers workspace SSO):

    {PUBLIC_BASE_URL}/api/v1/auth/oauth/google/callback

**Sign in and sign up.** `GET /auth/oauth/google/login` signs an *existing* user in and never
creates anything. Adding `?mode=signup` (the sign-up page's "Continue with Google" button, optionally with
`&company_name=…`) lets a *new* verified email create its own workspace on the signup plan, with that person as
owner and no password (they can set one with "Forgot password"). If the email already has an account it is simply
signed in. Google requires `email_verified`. The button only appears when `GOOGLE_CLIENT_ID` and
`GOOGLE_CLIENT_SECRET` are set.

A provider with empty credentials is simply off: `POST /oauth/<provider>/authorize`
returns `400 {"detail": {"code": "provider_not_configured"}}` and the hub shows
"Not configured on this platform".

Endpoints: `GET /oauth/providers`, `POST /oauth/{provider}/authorize`,
`GET /oauth/{provider}/callback` (public), `DELETE /oauth/{provider}`,
`GET /oauth/{provider}/resources`, `POST /oauth/{provider}/select`,
`GET /oauth/whatsapp/config`, `POST /oauth/whatsapp/embedded-signup`,
`GET /auth/oauth/{google|microsoft}/login` + `/callback`,
`POST /crm/leads/{id}/push-to-crm`.

## Google (Calendar, optional Sheets, Sign in)

1. console.cloud.google.com > create a project > **APIs & Services > Library**:
   enable *Google Calendar API* (and *Google Sheets API* if tenants use Sheets).
2. **OAuth consent screen**: External, add your domain, privacy-policy and
   terms links. Add scopes `calendar.events`, `calendar.calendarlist.readonly`,
   `calendar.freebusy` (+ `spreadsheets` for the optional Sheets bundle).
3. **Credentials > Create OAuth client ID > Web application**. Authorized
   redirect URIs: `.../api/v1/oauth/google/callback` and
   `.../api/v1/auth/oauth/google/callback`.
4. `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`.

Needs provider approval: Calendar scopes are *sensitive* and `spreadsheets` is
*sensitive*; while the app is in "Testing" only listed test users can connect
and refresh tokens expire after 7 days. Submit for Google OAuth verification
before onboarding real tenants (a short demo video is required).

## Microsoft (Outlook calendar, mail, Sign in)

1. portal.azure.com > **Microsoft Entra ID > App registrations > New**.
   Supported account types: *Accounts in any organizational directory and
   personal Microsoft accounts* (tenant `common`).
2. **Authentication > Add platform > Web**: redirect URIs
   `.../api/v1/oauth/microsoft/callback` and
   `.../api/v1/auth/oauth/microsoft/callback`.
3. **Certificates & secrets**: new client secret.
4. **API permissions > Microsoft Graph > Delegated**: `User.Read`,
   `Calendars.ReadWrite`, `Mail.Send`, `offline_access`, `openid`, `email`, `profile`.
5. **Token configuration > Add optional claim > ID token > `xms_edov`**
   (email domain owner verified). Required for *Sign in with Microsoft* with
   work/school accounts: Microsoft does not verify the `email` claim itself, and
   we refuse to log anyone in on an unverified email. Personal accounts work without it.
6. `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET` (`MICROSOFT_TENANT_ID` optional).

Needs provider approval: none to start. Tenants whose Entra admin disabled user
consent must have an admin grant consent once. Publisher verification is
recommended to avoid the "unverified" warning.

## Salesforce

1. Setup > **App Manager > New Connected App**; enable OAuth settings.
2. Callback URL: `.../api/v1/oauth/salesforce/callback`.
3. Scopes: *Manage user data via APIs (api)*, *Perform requests at any time
   (refresh_token, offline_access)*, *Access the identity URL service (id)*.
4. Leave "Require PKCE" ON (we send it); "Require Secret for Web Server Flow" ON.
5. `SALESFORCE_CLIENT_ID` (Consumer Key), `SALESFORCE_CLIENT_SECRET` (Consumer
   Secret). `SALESFORCE_LOGIN_URL=https://test.salesforce.com` for sandboxes.

Needs provider approval: a Connected App is created in *your* org; for other
customers' orgs to authorize it, package it (managed or unmanaged package) or
have each customer admin install it. Newly created Connected Apps can take a few
minutes before the consumer key works. Customer admins may restrict "Connected
Apps" use to pre-approved apps.

## HubSpot

1. developers.hubspot.com > **Apps > Create app**, *Auth* tab.
2. Redirect URL: `.../api/v1/oauth/hubspot/callback`.
3. Scopes: `oauth`, `crm.objects.contacts.read/write`, `crm.objects.deals.read/write`.
4. `HUBSPOT_CLIENT_ID`, `HUBSPOT_CLIENT_SECRET`.

Needs provider approval: none to connect. Listing in the HubSpot marketplace is optional.

## Zoho CRM

1. api-console.zoho.in (or .com / .eu / .com.au ...) > **Add Client > Server-based Applications**.
2. Authorized Redirect URI: `.../api/v1/oauth/zoho/callback`.
3. Scopes requested: `ZohoCRM.modules.ALL`, `ZohoCRM.users.READ`, `AaaServer.profile.READ`
   (offline access; refresh token).
4. `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_ACCOUNTS_DOMAIN` (the data
   centre where you registered the client, e.g. `https://accounts.zoho.in`).
   Zoho reports each tenant's own data centre on the callback; their API domain
   (`www.zohoapis.in/.eu/...`) is stored per connection.

Needs provider approval: none. A client registered in one data centre only works
for users whose account is in that data centre: register the client in each
region you serve (one app per region needs separate credentials; today a single
client id/secret pair is supported).

## Slack

1. api.slack.com/apps > **Create New App > From scratch**.
2. **OAuth & Permissions**: redirect URL `.../api/v1/oauth/slack/callback`.
   Bot Token Scopes: `chat:write`, `chat:write.public`, `channels:read`, `groups:read`.
3. `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`. To use "Manage distribution" turn
   on *Activate Public Distribution*. Token rotation is supported if enabled.

Needs provider approval: none for installing into any workspace once
distribution is activated; the Slack Marketplace listing is optional. To post
to a private channel, invite the bot (`/invite @app`).

## Meta: WhatsApp Embedded Signup

Implemented: `GET /oauth/whatsapp/config` (app id + config id for the JS SDK)
and `POST /oauth/whatsapp/embedded-signup` (exchanges the signup `code`,
reads the number, subscribes the app to the WABA, registers the number for
the Cloud API, stores `whatsapp_accounts` with the token encrypted). The hub
launches the Facebook JS SDK `FB.login` with your configuration id.

Not something this repo can provide; you must have: a Meta **Tech Provider**
app (App Review for `whatsapp_business_management` and
`whatsapp_business_messaging`, plus Business Verification), an **Embedded
Signup configuration** (Facebook Login for Business > Configurations > WhatsApp
Embedded Signup) whose id goes in `META_WHATSAPP_CONFIG_ID`, the WhatsApp
webhook pointing at `{PUBLIC_BASE_URL}/api/v1/webhooks/whatsapp/meta`, and
`https://<frontend>` listed under *Facebook Login > Allowed Domains for the
JavaScript SDK*. The number's two-step-verification PIN is generated on first
registration and returned once in the response (store it); you can pass your
own 6-digit `pin`. This path could not be exercised against real Meta here
(mocked in tests only).

## Shopify

1. partners.shopify.com > **Apps > Create app** (custom distribution is fine).
2. **Configuration > App URL / Allowed redirection URL(s)**:
   `.../api/v1/oauth/shopify/callback`.
3. Scopes: `read_orders`, `read_customers`, `read_products`.
4. `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET`.

The tenant enters their store domain (`my-store.myshopify.com`); the callback
is verified with Shopify's HMAC signature, and the shop must match the one in
the signed `state`. The offline token is mirrored into a `Shopify (OAuth)`
connector so the `lookup_order` AI tool works unchanged.

Needs provider approval: protected customer data (names/addresses/emails in
orders) requires a *Protected customer data access* request; `read_all_orders`
(orders older than 60 days) also needs approval. Public listing needs app review.

## What each connection powers

| Provider | Used for |
|---|---|
| Google / Microsoft | Appointment booked / rescheduled / cancelled creates / updates / deletes a calendar event (best effort, never breaks booking). Optional `block_busy` hides slots that clash with their calendar. |
| HubSpot / Zoho / Salesforce | Creating a CRM lead (UI or AI tool) upserts the contact / lead; `POST /crm/leads/{id}/push-to-crm` pushes on demand. Result stored in `lead.custom_fields.crm_sync`. |
| Slack | `ticket.created` and `sla.breached` (selectable) post to the chosen channel via the connector dispatch. |
| Shopify | Order lookup tool. |

## Security notes

* `state` is an HS256 JWT (JWT secret) with tenant, user, provider, return URL
  and nonce, valid 10 minutes, single use (row in `oauth_states`, burned before
  the code exchange). PKCE (S256) is used for Google, Microsoft, Salesforce; the
  verifier never leaves the server.
* `return_url` must be a relative path; anything else falls back to `/integrations/hub`.
* The OAuth callback is public by necessity; tenant identity comes only from the signed state.
* Sign in with Google / Microsoft never creates users or tenants and refuses
  accounts with two-factor enabled (use password + code there).

## Connecting with an API token instead of sign-in

Sign-in (OAuth) needs the platform to register an app with the provider. When
that has not been done, or the provider has no sign-in for private accounts,
a workspace owner can paste a token instead. It is the same connection from
then on: CRM push, Slack alerts and the Shopify connector do not know the
difference.

| Provider | What the user pastes | How it is checked |
| --- | --- | --- |
| HubSpot | Private app access token | `GET /account-info/v3/details` |
| Shopify | Admin API access token + store domain | `GET /admin/api/.../shop.json` |
| Slack | Bot token (`xoxb-…`) | `auth.test` |

`POST /oauth/{provider}/token` verifies the token against the provider first.
A refused token (401/403) returns `invalid_token` (422); an unreachable
provider returns `provider_unreachable` (502). Nothing is saved in either
case. A good token is stored encrypted (`encrypt_secret`) as an
`OAuthConnection` with `extra.manual = true`, is never returned by any
endpoint, and the card shows "Connected with an API token you pasted".
Disconnecting deletes it locally; the provider-side token is not revoked
(we cannot), so the owner should remove it in the provider's dashboard too.

## Workspace email sending key (Resend or Brevo)

`POST /integrations/email-sender` stores a workspace's own Resend or Brevo
API key (encrypted) with the address and name to send as. It is checked live
first: Resend must list the sending domain as verified; Brevo must list the
sender as active. Campaigns and customer-facing email use this key when the
workspace has no connected mailbox; otherwise the platform sender is used.

Known limits:
- Bounce and complaint webhooks for a workspace's own Resend/Brevo account
  are not received (the webhook secret is platform-level), so those bounces
  are not auto-suppressed. Unsubscribes and manual suppression still work.
- System mail (post-call summaries, operations follow-ups) still uses the
  platform sender.
