# Call Center Platform — Backend

Multi-tenant AI call center platform backend: Postgres schema, FastAPI
business plane, a Go realtime voice gateway, AI agents with a tool registry,
a workflow engine, and WhatsApp/Razorpay integrations. See "What's not here
yet" for what's deliberately out of scope.

## Stack

- **API**: FastAPI (async), SQLAlchemy 2.0 (async, asyncpg), Pydantic v2
- **Realtime gateway**: Go — Twilio Media Streams in, Sarvam STT/TTS out
- **Background jobs**: Celery (Redis broker) — workflow execution, campaigns
- **DB**: PostgreSQL (`pgvector/pgvector:pg16` image — ready for embeddings later)
- **Migrations**: Alembic
- **Auth**: JWT access/refresh tokens (PyJWT), bcrypt password hashing
- **Voice**: Twilio (call carrier) + Sarvam (STT, TTS, and the agent's LLM)
- **Messaging**: Meta WhatsApp Cloud API (Tech Provider model)
- **Payments**: Razorpay Payment Links
- **Package managers**: [uv](https://docs.astral.sh/uv/) (Python), Go modules

## Architecture

```
                    PHONE CALL                              WHATSAPP MESSAGE
                        │                                          │
                    Twilio (carrier)                        Meta Cloud API
                        │                                          │
                        ▼                                          │
              Go realtime gateway                                  │
              (apps/realtime)                                      │
              Twilio Media Streams  ◄──────┐                       │
              Sarvam STT/TTS               │ audio in/out           │
                        │                  │                       │
                        │ transcript       └───────────────────────┤
                        ▼                                          ▼
              FastAPI "AI Engine"  ◄───────────────────────────────┘
              (app/services/agent_engine.py)
                        │
              Sarvam chat/LLM + tool-calling loop
                        │
                        ▼
              Tool registry (app/tools/)
              get_customer, create_ticket, send_whatsapp_message,
              create_customer_payment_link, transfer_call, ...
                        │
                        ▼
              PostgreSQL (tenant-scoped)
                        │
              CALL_ENDED / TICKET_CREATED / ... trigger
                        ▼
              Workflow engine (app/workflows/) ── Celery ──► WhatsApp / Razorpay / tickets
```

The Go gateway and FastAPI are separate processes (separate deploys even),
talking over a small internal HTTP API (`app/api/v1/internal.py`) guarded by
a shared service token — see "Realtime voice" below for exactly what each
side owns.

## Repository layout

```
apps/api/app/
  core/            config, security (JWT/bcrypt), deps (auth/tenant/RBAC),
                   permissions, crypto (Fernet), internal_auth, repository
  api/v1/          route modules — auth, users, tenants, customers, tickets,
                   calls, agents, telephony (Twilio webhooks), whatsapp,
                   payments, workflows, campaigns, internal (gateway-only)
  schemas/         Pydantic request/response models
  tools/           the tool registry AI agents (and workflows) call
  services/        agent_engine.py — the LLM tool-calling turn loop
  workflows/       engine.py (interpreter) + dispatch.py (trigger lookup)
  integrations/    Twilio, Sarvam, WhatsApp, Razorpay clients
  worker/          Celery app + tasks (workflow triggers, campaigns)
apps/realtime/     Go realtime gateway (separate module, separate deploy)
  internal/twilio/    Media Streams message types
  internal/sarvam/    STT (streaming WS) + TTS (REST) clients
  internal/backend/   HTTP client for FastAPI's internal/* endpoints
  internal/session/   per-call turn orchestration
packages/database/  Shared SQLAlchemy layer (imported by the API and workers,
  models/            not by the Go gateway — it never touches Postgres directly)
migrations/          Alembic environment + versions
infrastructure/docker/  Dockerfiles (api, realtime) + api entrypoint
tests/               pytest + httpx ASGI tests (Python); apps/realtime has
                     its own `go test ./...` for the message-protocol layer
```

## Tenant isolation — the load-bearing design decision

Every tenant-owned table carries a `tenant_id` via `TenantScopedMixin`
([packages/database/mixins.py](packages/database/mixins.py)). `tenant_id` is
**never** read from a request path/query/body in the tenant-facing API.
Instead:

1. A JWT encodes `membership_id` (a `tenant_users` row), not a raw `tenant_id`.
2. [`get_current_context`](apps/api/app/core/deps.py) loads that membership
   from the DB on every request, verifying it's still active, and returns a
   `RequestContext` carrying the verified `tenant_id`, role, and permission set.
3. [`TenantScopedRepository`](apps/api/app/core/repository.py) takes
   `tenant_id` from `RequestContext` in its constructor and bakes it into
   every query — a route can't forget to filter.

RBAC works the same way: `require_permission(code)` is a dependency factory
checked against the role loaded in step 2, not against anything client-supplied.

**Two other trust boundaries exist, both narrower and both documented at the
code they guard:**
- **Inbound Twilio webhooks** ([app/api/v1/telephony.py](apps/api/app/api/v1/telephony.py)):
  tenant_id comes from a `phone_numbers` lookup on Twilio's "To" number, and
  the request itself is verified via Twilio's request-signature scheme.
- **The Go gateway's internal/* calls** ([app/core/internal_auth.py](apps/api/app/core/internal_auth.py)):
  a shared service token proves "this is our own realtime plane", but
  tenant_id still only ever comes from the `Call` row looked up by `call_id`
  — the token never grants a tenant scope by itself.

Postgres row-level security is the second fence under those repositories.
It is on for tenant-authenticated requests. The API has to connect as a
role that is not a superuser, or Postgres ignores the policies. See
[Human handoff, recording, roles, RLS](#human-handoff-recording-roles-rls).

## Realtime voice — how a call actually flows

1. Twilio hits `POST /api/v1/webhooks/telephony/twilio/voice`. FastAPI looks
   up the tenant/agent by the dialed number (`phone_numbers`), creates a
   `Call` row, and returns TwiML connecting the call to the Go gateway's
   WebSocket (`<Connect><Stream>`, passing `callId`/`tenantId` as custom parameters).
2. Twilio streams raw audio (mulaw/8kHz) to the Go gateway over that
   WebSocket. The gateway forwards it live to Sarvam's streaming STT.
3. On a final transcript, the gateway calls FastAPI's
   `POST /internal/calls/{id}/turn` with the text. FastAPI (the "AI Engine")
   loads the agent's config + conversation history, runs Sarvam's chat
   completion with the agent's enabled tools, executes any tool calls
   (tenant-scoped, against the tool registry — never raw SQL/HTTP from the
   model), and returns the reply text.
4. The gateway sends that text to Sarvam's TTS (mulaw/8kHz out, so no
   transcoding either direction) and plays it back to the caller. A partial
   transcript arriving while the agent is mid-reply triggers a Twilio
   `clear` message — barge-in.
5. On hangup, the gateway calls `POST /internal/calls/{id}/end`, which
   finalizes the `Call` row and enqueues any `CALL_ENDED` workflows via Celery.

A `transfer_call` tool invocation doesn't get handled by the Go gateway at
all: FastAPI (which holds the Twilio credentials and the call's
`provider_call_sid`) redirects the live call itself right after the turn
that requested it.

**Verified against a live Sarvam API key** (2026-09-18): mulaw/8kHz is
genuinely accepted by realtime STT (`session.begin` echoes it back), a full
TTS→STT round trip transcribed "Hello, thanks for calling." back correctly,
and the chat API's `tool_calls` shape is exactly the OpenAI convention this
code assumes (verified with a real tool-call + tool-result round trip). Two
real bugs turned up and were fixed:
- `bulbul:v2` (the TTS model this code used) is deprecated — now `bulbul:v3`.
- The chat model reliably drops the leading `+` from phone numbers passed as
  tool arguments even in E.164 — `get_customer`'s handler
  ([app/tools/handlers.py](apps/api/app/tools/handlers.py)) now normalizes
  before querying, since `customers.phone` is always stored with it.

**Still unverified**: this was tested with synthetic audio (Sarvam's own TTS
output fed back into its STT), not a real phone call through Twilio, and the
chat model is a reasoning model — it emits a variable amount of internal
`reasoning_content` before answering (measured 1.3s and 233–873 tokens for
the same trivial prompt across two calls). That's a real latency variance to
budget for in a live call; test with real call volume before assuming
consistent turn-taking latency.

## Multi-tenant setup — how *other* tenants get a number and credentials

This is a platform meant for many tenants, so nothing above should require
you (the platform operator) to hand-hold every new signup through the
Twilio console. Two independent things a tenant needs, both self-service:

**Getting a phone number** — the workspace connects its own account first.
`GET /api/v1/integrations/setup` says what is still missing. An Indian number
is an ExoPhone bought in that workspace's own Exotel account, then connected
with `POST /api/v1/integrations/exotel` and added under Phone numbers. A
Twilio number is bought only after `POST /api/v1/integrations/twilio`:

```
GET  /api/v1/phone-numbers/available?country=US&area_code=415
POST /api/v1/phone-numbers/provision  {"phone_number": "+1415...", "ai_agent_id": "..."}
```

The search and the purchase use that workspace's Twilio account. They are
refused while no account is connected, so a client is never billed to a
shared Twilio account. The voice assistant uses the Sarvam key that workspace
saved with `POST /api/v1/integrations/sarvam`, checked with Sarvam before it
is stored. WhatsApp uses the number that workspace connected.

**Bringing their own Twilio account or Sarvam key** (optional, e.g. an
enterprise tenant that wants its own Twilio billing/data-residency):
```
POST   /api/v1/integrations/twilio  {"account_sid": "AC...", "auth_token": "..."}
POST   /api/v1/integrations/sarvam  {"api_key": "..."}
GET    /api/v1/integrations
DELETE /api/v1/integrations/{provider}
```
Once connected, **every** Twilio/Sarvam call made on that tenant's behalf —
outbound calls, webhook signature validation, live-call transfer redirects,
phone number search/purchase, and the realtime gateway's own STT/TTS for
that tenant's calls — automatically resolves to their credentials instead of
the platform's. This resolution is centralized in
[app/integrations/credentials.py](apps/api/app/integrations/credentials.py);
every Twilio/Sarvam call site in the codebase goes through it rather than
reading `app.core.config` directly, which is what makes "many tenants"
actually true structurally rather than by convention. Secrets are encrypted
at rest the same way as WhatsApp tokens (Fernet, `app/core/crypto.py`).

For the Go gateway specifically: since it never touches Postgres directly,
it fetches the resolved Sarvam key (and the agent's language) once per call
via `GET /internal/calls/{id}/config` rather than relying on its own
`SARVAM_API_KEY` env var — that env var is only the fallback if the config
fetch itself fails.

## AI agents & tools

`ai_agents` holds an agent's name/language/voice/system prompt per tenant.
`ai_agent_tools` says which tools (from the fixed, code-level registry in
[app/tools/registry.py](apps/api/app/tools/registry.py)) that agent may call:
`get_customer`, `list_recent_tickets`, `create_ticket`, `add_customer_note`,
`schedule_callback`, `send_whatsapp_message`, `create_customer_payment_link`,
`transfer_call`. A tenant can enable/disable tools per agent; it can't
redefine what a tool does — the registry is platform code, not tenant
config, which is what keeps "the LLM can only do what a real tool lets it
do" true regardless of the tenant's system prompt.

The same registry backs the workflow engine's actions (see below) — a
workflow's `create_ticket` action and an AI agent's `create_ticket` tool
call are the exact same handler.

## Workflow engine

`workflows.definition` is a small ordered list of nodes — a linear pipeline
(`condition -> action -> condition -> action`), not a general branching DAG;
see the module docstring in
[app/workflows/engine.py](apps/api/app/workflows/engine.py) for why that
scope call was made. Triggers today: `call_ended`, `ticket_created`,
`customer_created`, `message_received` (the latter two aren't wired to an
emitter yet — only `call_ended` is fired, from the internal call-end
endpoint). Runs execute via Celery, not inline in the request that fired the
trigger, so a slow action (a WhatsApp send, a Razorpay call) never blocks a
call turn or webhook response.

## WhatsApp

One Meta App/webhook shared across all tenants (the "Tech Provider" model);
each tenant's own `phone_number_id` + access token live in
`whatsapp_accounts`, with the token encrypted at rest
([app/core/crypto.py](apps/api/app/core/crypto.py), Fernet). An inbound
message runs through the exact same `agent_engine.run_turn` as a voice
call — same tools, same conversation-history model, different channel.

## Payments (Razorpay)

Scoped deliberately as a **business tool**, not platform billing: an AI
agent (or staff, via `POST /api/v1/payment-links`) creates a Razorpay
Payment Link to collect payment from a tenant's own customer during/after a
call. There's no subscription/invoicing system for charging *tenants* for
platform usage — that's a distinct, larger piece of scope (Phase 7 in the
original design) that wasn't part of this pass.

## Local setup

Requires Docker (for Postgres/Redis), [uv](https://docs.astral.sh/uv/), and
Go 1.23+ if you're touching the realtime gateway.

```bash
cp .env.example .env
# generate real secrets before anything but local dev — see comments in
# .env.example for the exact command for each of JWT_SECRET_KEY,
# ENCRYPTION_KEY, and INTERNAL_SERVICE_TOKEN.

docker compose up -d postgres redis   # ports 5433/6380 on the host — see note below
uv sync

export PYTHONPATH=".:apps/api"        # or just use `make ...` targets, which set this
make migrate                          # alembic upgrade head (schema + seeded roles/permissions)
make dev                              # uvicorn --reload on :8000
```

Then hit `http://localhost:8000/docs` for interactive API docs, or:

```bash
curl -X POST http://localhost:8000/api/v1/auth/signup -H "Content-Type: application/json" -d '{
  "company_name": "Acme Dental", "tenant_slug": "acme-dental",
  "full_name": "Alice Owner", "email": "alice@acmedental.com", "password": "supersecret1"
}'
```

**Port note**: `docker-compose.yml` maps Postgres to host port `5433` and
Redis to `6380` (not the standard 5432/6379) to avoid clashing with a
locally-installed Postgres/Redis. Remap to the standard ports if you don't
have those running natively.

**Going live with real calls/messages/payments** needs, at minimum: a
Twilio account (`TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`), a Sarvam API key
(`SARVAM_API_KEY`), a public HTTPS tunnel to this API for Twilio/Meta
webhooks (`PUBLIC_BASE_URL`) and to the realtime gateway
(`REALTIME_WS_URL`), and — for WhatsApp/Razorpay specifically — a Meta app
secret/verify token and Razorpay keys. See `.env.example` for the full list
with generation commands.

### Running the whole stack in Docker

```bash
make up      # docker compose up -d --build — api + worker + realtime + postgres + redis
make down
```

`api` runs `alembic upgrade head` on startup then serves on `:8000`.
`worker` runs the Celery worker (workflow triggers, campaigns) against the
same image. `realtime` builds the Go gateway and serves on `:9000`. `apps/`,
`packages/`, and `migrations/` are bind-mounted into the Python services for
local iteration; rebuild (`make up`) after changing dependencies.

### Creating a new migration

```bash
make revision m="add campaigns table"
```

Review the autogenerated file before running `make migrate` — in particular,
Alembic renders unnamed-constraint `drop_constraint(None, ...)` lines for
foreign keys that need a name added before the downgrade will actually run;
every migration in this repo so far has needed that one manual fix.

### Working on the realtime gateway

```bash
cd apps/realtime
go build ./...   # compile check
go test ./...    # message-protocol unit tests (no live Twilio/Sarvam needed)
gofmt -l .        # should print nothing
```

## Tests

```bash
docker exec <postgres-container> psql -U callcenter -d callcenter -c "CREATE DATABASE callcenter_test;"
make test
```

Tests hit a real Postgres (`callcenter_test`) through the full ASGI app via
httpx — no mocked DB — for everything that doesn't need a live external
provider. What's covered: tenant isolation and RBAC
([tests/test_tenant_isolation.py](tests/test_tenant_isolation.py)), the AI
agent/tools API and its own tenant isolation
([tests/test_agents_api.py](tests/test_agents_api.py)), the inbound Twilio
webhook creating calls and rejecting unknown numbers
([tests/test_telephony_webhook.py](tests/test_telephony_webhook.py)), the
workflow engine's condition/action interpreter
([tests/test_workflow_engine.py](tests/test_workflow_engine.py)), tool
handlers' own tenant scoping ([tests/test_tools.py](tests/test_tools.py)),
and WhatsApp/Razorpay webhook signature verification
([tests/test_webhook_signatures.py](tests/test_webhook_signatures.py)).

What's *not* covered by automated tests, because it needs real provider
credentials: an actual Sarvam STT/TTS/chat round-trip, an actual Twilio
call, an actual WhatsApp message send, an actual Razorpay payment link.

## Human handoff, recording, roles, RLS

- **Human handoff** — `/api/v1/transfer-targets` holds each tenant's human
  numbers. When an agent calls `transfer_call`, every active target rings at
  once (first to answer wins, up to 5, lowest `priority` first); if nobody
  answers the caller hears an apology and the call ends. With no targets it
  falls back to `TRANSFER_FALLBACK_NUMBER`. Each handoff is logged as a
  `TRANSFER_REQUESTED` call event.
- **Call recording** — opt-in per tenant (`PATCH /tenants/me`
  `call_recording_enabled`, off by default; the tenant is responsible for
  any "this call may be recorded" disclosure). Recording starts when the call
  connects; Twilio's `/webhooks/telephony/twilio/recording` callback stores
  the URL, and `GET /calls/{id}/recording` streams the audio through the API
  (Twilio URLs need credentials) and audit-logs every access. Transcripts are
  at `GET /calls/{id}/transcript`.
- **Custom roles** — `/api/v1/roles` and `/api/v1/permissions`. System roles
  are read-only and their names reserved; you can only grant permissions you
  hold yourself; a role in use cannot be deleted.
- **Postgres row-level security** — migration `e6f0b3d8c2a5` (logic in
  `packages/database/rls.py`). Tenant-authenticated requests set
  `app.tenant_id` per transaction, so a query that forgets its tenant filter
  returns nothing from other tenants. Policies allow everything when no tenant
  is set (webhooks, workers, signup) and platform admins are exempt.
  **The app must connect as a normal role: Postgres superusers and
  BYPASSRLS roles ignore RLS** (managed Postgres' default `postgres` user is
  a superuser — create a dedicated app role). Run the suite under RLS with
  `TEST_RLS=1 pytest`.

## Answering-machine detection

Per AI agent: set `detect_voicemail: true` (and optionally `voicemail_message`)
via `POST/PATCH /api/v1/ai-agents`. Outbound calls placed for that agent (REST,
campaigns, MCP `trigger_call`) use Twilio's asynchronous AMD, so humans hear no
delay. When Twilio reports a machine (`/webhooks/telephony/twilio/amd`), the call
is hung up, or, if the greeting has finished and a `voicemail_message` is set, that
message is spoken (Twilio's default voice) and the call ends. The verdict is stored
as `calls.answered_by`; machine-answered calls get `outcome = "voicemail"`.

## CSAT surveys

Off by default; configure with `PATCH /api/v1/csat/settings` (`settings.manage`).
When enabled, a finished call (at least `min_call_seconds` long) or a ticket moved
to resolved/closed schedules one survey for that customer, sent `delay_minutes`
later by the `csat.send_due_surveys` Celery beat job (every minute). `channel` is
`auto` (WhatsApp while its 24-hour window is open, otherwise SMS), `whatsapp` or
`sms`; SMS needs the SMS channel enabled so replies reach us. The customer replies
with `1`-`5` plus an optional comment: it is captured before the AI sees the
message, answered with `thank_you`, stored on the survey, and fires a
`csat.received` webhook. Ratings at or below `low_score_ticket_threshold` open a
high-priority ticket. A customer is surveyed at most once per `cooldown_hours`,
and unanswered surveys expire after `expire_hours`. Results: `GET /api/v1/csat/summary`
(average, CSAT % = share of 4-5, distribution, response rate, call vs ticket) and
`GET /api/v1/csat/surveys`; the MCP tool `get_csat_summary` returns the same.
Survey text is whatever you set in `question`, so write it in your customers' language.

## Voice latency and natural speech

How a reply reaches the caller, in order of what saves the most time (measured live
against Sarvam on 2026-10-15):

1. **Persistent text-to-speech WebSocket** (`TTS_WEBSOCKET`, default on). One
   Bulbul v3 socket per language is opened when the call starts, so a sentence's
   first audio arrives in ~0.23s instead of ~0.9-1.3s for a REST request (which
   also pays a TLS handshake each time). Falls back to REST by itself.
2. **Streamed replies** (`STREAM_REPLIES`, default on). The gateway calls
   `POST /internal/calls/{id}/turn/stream`; the LLM's tokens are cut into sentences
   (`app/services/speech_stream.py`) and each sentence goes to TTS as soon as it is
   written. Agents with guardrail rules are not streamed, because a rule can veto a
   finished reply. Browser test sessions and any error fall back to the normal turn.
3. The conversation model (`sarvam-105b-conversations`) answers in ~0.4s;
   streaming gets its first sentence out in ~0.2s.

Rough effect on time to first word after the caller stops talking: ~1.4s before,
~0.5s now (the backend's own DB work is not included). `bulbul:v4-flash` was
measured too and is no faster than v3 over REST (~1.0-1.2s), so the voice model
was left on v3; v4-flash uses different voice ids (`name_language_style`).

To make replies sound like a person rather than an assistant, voice calls add
`HUMAN_VOICE_RULES` to the prompt (natural reactions, no stock phrases such as
"I understand your concern", spoken numbers, one language per reply, mixing only
as the caller does), tell the agent its voice's gender so Hindi verbs agree
(`karti`/`karta hoon`), rotate the "one moment" filler phrases, and require the
agent to admit it is an AI when asked. `scripts/eval_voice_style.py` compares
prompts against the live model.

## Plans, free tier and limits

Every workspace has a plan tier (`tenants.plan_tier`): **Free**, **Starter**, **Growth**,
**Scale**, **Enterprise**, or **legacy** (unlimited; what workspaces that predate plans get).
The free plan is **10 AI call minutes in total** (one-time), at most 5 minutes per call, 50 WhatsApp
messages, 20 SMS segments, 30 customer emails, 1 agent, 1 phone number, 2 seats, 5 knowledge documents,
1 API key, no campaigns and no recording. A free workspace cannot buy a number on the platform Twilio
account, and a number the platform bought is released on delete and after 14 idle days. Calls on that
number, including a human queue, draw the same minute pool. Limits are enforced where the money is
spent: the call-time cap, outbound and inbound admission, record creation, WhatsApp, SMS and email.
A reached limit is an HTTP 402 with `{detail, code, tier, upgrade_path}`. An unverified owner on a
non-legacy workspace gets 403 `email_unverified` before any of those spends.

* The tiers, prices and every limit live in [`apps/api/app/services/plans.py`](apps/api/app/services/plans.py).
* Why those numbers (provider costs, margins, free-tier worst case, what was wrong before):
  [`docs/BILLING.md`](docs/BILLING.md). Re-run `python scripts/cost_model.py` when a price changes.
* New sign-ups start on `SIGNUP_PLAN_TIER` (default `free`). A subscription moves a workspace up
  (Razorpay webhook); a platform admin can grant extras with
  `PUT /api/v1/platform/admin/tenants/{id}/plan`. `GET /api/v1/billing/entitlements` feeds the Billing page.
* Allowances are hard caps. `allow_overage` (a platform-admin override) lets a workspace past the minute, WhatsApp, SMS and email caps and writes the extra onto a pending invoice. Collecting that invoice in Razorpay is still manual. Legacy workspaces stay unlimited until a person moves them.

## What's not here yet

- **A real semantic embedding model.** Search already mixes Postgres full-text
  with a local 384-number hash embedder and a small synonym list, and pgvector
  storage is optional. PDF, DOCX, HTML and CSV upload already work. What is
  missing is a hosted embedding model that understands meaning the words do
  not share.
- **Collecting an overage invoice automatically.** The invoice is written. Razorpay
  does not charge the card for it yet.
- **Live provider round-trips** in the automated suite. Twilio, Sarvam,
  WhatsApp, Razorpay and Meta are mocked. A staging call is still the check
  that a real number, a real voice and a real payment work together.
- **Kafka, ClickHouse, Temporal** — later-phase scale infrastructure; Celery
  + Postgres is the right size for this stage.
