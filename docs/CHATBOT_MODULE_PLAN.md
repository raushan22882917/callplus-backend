# Chatbot module: gap table and phased plan

Audit of this repo against the "Convoro-style" feature list. Nothing has been built yet. This is from a scan of models, services and routes, not a line-by-line review; "exists" means a working feature is present, and each phase starts by reading the code it extends.

## Our stack (the placeholder, filled in)

- **Backend:** Python 3.12, FastAPI, async SQLAlchemy, Alembic (single head), Postgres with pgvector extension available but unused, Redis, Celery workers. Tests: pytest against real Postgres/Redis.
- **Frontend:** Next.js 16 (app router), Tailwind 4, TanStack Query, Vitest. Separate folder, not a git repo.
- **Tenancy:** `tenant_id` on every table, Postgres row-level security keyed on `app.tenant_id`, `require_permission(...)` on routes, audit log, per-tenant roles.
- **Voice/AI:** Twilio telephony, Sarvam (STT, TTS, chat model) as the **only** model provider. Web chat widget, WhatsApp, SMS, email-to-conversation already exist.
- **Deploy:** Railway (api, worker, realtime, frontend), migrations run on API start.

## Gap table

| # | Feature | Status | Where it plugs in |
|---|---|---|---|
| 1 | Workspaces, roles, permission matrix | **Exists** | `rbac`, `Permissions`, custom roles. Reuse; add `chatbot.*` permissions, no second user table. |
| 1 | Email+password, TOTP, invitations, API keys with scopes, RLS, audit log | **Exists** | `auth`, `platform_totp`, `developer`, `rls.py`, `audit`. |
| 1 | Magic-link login | **Missing** | `auth.py` (small). Needs your OK: it touches the auth model. |
| 2 | Many agents, greeting, guardrails, languages, tools | **Exists** | `AIAgent`, guardrails, tools registry. |
| 2 | Business profile, suggested questions, fallback, allowed origins, escalation rules | **Partial** | Widget config has fallback, quick replies, origins; escalation rules exist in guardrails. Fold into one validated config. |
| 2 | Draft vs published, publish gate (paid plan + ready source) | **Missing** | New `status` on the chat agent; reuse `plans.effective()` for the gate. |
| 3 | Knowledge: text and file upload | **Partial** | `KnowledgeDocument/Chunk`, text-like uploads only. Add PDF/Word/HTML. |
| 3 | Website crawl (same-site, robots, sitemap, weekly re-sync) | **Missing** | New Celery task; shared SSRF-safe fetch. |
| 3 | Embeddings + hybrid retrieval (vector + FTS, RRF), synonyms, quality gate | **Missing** | Today: Postgres FTS only. Add `embedding vector` column + RRF in `services/knowledge.py`. |
| 3 | Pluggable embedder, live status per source | **Missing** | Local hashing embedder for dev; external API optional. |
| 3 | Citations on answers | **Missing** | `KnowledgeSearchHit` exists; return it with the reply. |
| 4 | One turn pipeline for every channel | **Partial** | `agent_engine.run_turn / run_turn_stream` already serve voice, WhatsApp and web chat. Add credit/spend-cap check, retrieval-first, no-match fallback, citations. |
| 4 | Model providers behind one interface | **Missing** | Sarvam only. Add Anthropic and OpenAI-compatible adapters plus a no-key demo model, with retry. |
| 4 | Tools: server HTTP actions, confirmation pause, client-side actions | **Partial** | Tool registry and loop exist (calendar, CRM, payments). Add generic HTTP-action tool, confirm-before-risky, browser actions. |
| 4 | SSE streaming on the public widget | **Partial** | Streaming exists on the internal voice path only. |
| 4 | PII redaction incl. Aadhaar/PAN/UPI; injection defences | **Partial** | `core/pii.py` has Aadhaar, card, phone, email. Add PAN and UPI; treat retrieved text as data in the prompt. |
| 4 | Idempotency keys on chat | **Partial** | Exists on `POST /calls`; add to chat. |
| 5 | Handoff triggers (anger, keyword, low confidence, ask for human) | **Exists** | `guardrails.py`, `transfer_call`, human takeover (`is_human_controlled`). |
| 5 | Inbox, assignee, status, notes, tags, priority, SLA, tickets, canned replies | **Exists** | `channels` inbox, `workspace`, `tickets`. Do **not** build a second inbox. |
| 5 | Bot silent while human owns thread; hand back | **Exists** | Check web-chat path specifically. |
| 5 | Thumbs up/down on answers | **Missing** | Per-message feedback column + widget control (CSAT exists per call/ticket). |
| 5 | Email-to-ticket | **Exists** | Inbound email channel. |
| 6 | Widget script + iframe, origin allow-list, theming, wizard, install check | **Exists** | `webchat_widget.py`, setup wizard. (Not an iframe today; CSP/iframe isolation is a decision below.) |
| 6 | Widget i18n (11 languages, RTL) | **Missing** | Widget strings are English. |
| 6 | Full-page chat link `/embed/<key>` | **Missing** | The `/webchat/try/<key>` test page is the seed. |
| 6 | Channels: WhatsApp, email | **Exists** | |
| 6 | Channels: Slack, Telegram | **Missing** | Only OAuth notification pieces exist. |
| 6 | Signed outbound webhooks, MCP server, public API with keys | **Exists** | `webhooks`, `mcp`, `developer`. |
| 6 | Voice | **Exists** | Telephony stack. The work is wiring `runTurn` + knowledge into it (below). |
| 7 | Guided onboarding wizard | **Partial** | Web-chat setup wizard exists. Extend with business profile, crawl, test, publish steps. |
| 8 | Dashboard screens | **Partial** | Agents, knowledge, channels, conversations, analytics exist. New: sources progress, playground with citations, bot analytics, unanswered questions. |
| 9 | Plans, credits, spend cap, Razorpay | **Partial** | `plans.py`, `cost_controls`, Razorpay payment links. Missing: credit packs, subscription webhooks, GST invoices (CGST/SGST/IGST, GSTIN check, FY numbering). Stripe: skip unless you want USD. |
| 10 | SSRF-safe fetch | **Partial** | `assert_public_https_url` for webhooks. Needs DNS pinning and per-hop redirect checks for the crawler. |
| 10 | Secret encryption | **Exists** | Fernet (not AES-GCM with tenant AAD). Acceptable; upgrade is optional. |
| 10 | Rate limits, privacy page, export/delete, retention | **Exists** | DPDP work done in this repo (`docs/DPDP_COMPLIANCE.md`). |
| 10 | Prometheus metrics, request ids | **Unchecked** | Verify before promising. |
| 11 | Dockerfiles, Railway, runbook | **Partial** | Railway deploy exists; runbook and backup drill missing. |

## Integration decisions (proposed)

1. **Identity:** reuse existing users/roles. Add permission codes `chatbot.read` / `chatbot.manage` mapped to existing roles. No new user table.
2. **Inbox:** bot chats are existing `Conversation`s with an owner field (`ai` / `human`); a handoff is a status change plus a ticket, not a new object.
3. **Voice:** keep Sarvam STT/TTS; call turns already go through `agent_engine`. Add retrieval with citations and show the retrieved passages to the human on warm transfer.
4. **Routing/SLA:** handoff uses the existing `routing` and ticket SLA settings.
5. **Customer context and write-back:** `customer_context` and post-call analysis already carry this; extend to chat.
6. **Agent assist:** expose `knowledge.search` as "suggested answers" in the inbox reply box.
7. **Reporting:** add containment, handoff and unanswered-question metrics to `cx_analytics`.
8. **Billing:** meter chat turns in the same usage events as calls; publish gate uses `plans.effective()`.
9. **Deployment:** same repo, one Alembic chain, tenant tables with RLS, a new Celery queue for crawl/embed.

## Phases

1. **Knowledge that answers well (backend).** Embedding column and hybrid retrieval (RRF, synonyms, quality gate), pluggable embedder, citations, PDF/Word/HTML extraction, source status. Tests: retrieval ranking, tenant isolation.
2. **Crawler and shared safe fetch.** robots/sitemap/same-site, page limit, weekly re-sync, SSRF-safe fetch with DNS pinning. Tests: private ranges, redirect hops, robots.
3. **Model providers and turn pipeline.** Provider interface (Sarvam, Anthropic, OpenAI-compatible, demo), retry, credit/spend-cap check, no-match fallback, injection hardening, PAN/UPI redaction, idempotency. Tests against fakes, plus one opt-in live check per provider.
4. **Draft/publish and public surfaces.** Agent status, publish gate, `/embed/<key>`, widget SSE streaming, feedback thumbs, i18n and RTL.
5. **Handoff and inbox merge.** Verify the web-chat silence-while-human path, bot/human owner field, knowledge suggestions in the inbox, bot metrics in analytics.
6. **Onboarding wizard v2 and dashboard screens.**
7. **Billing and compliance.** Credit packs, subscription webhooks, GST invoices.
8. **Channels and ops.** Slack, Telegram, runbook, backup drill.

Each phase ends with typecheck, lint and tests, reported as run, including failures.

## Decisions needed before code

1. **Paid services.** Phases 1 and 3 need an embeddings API (optional; a local hashing embedder works for dev) and Anthropic/OpenAI keys for real answer quality. Which, if any, may I add? The demo model is for tests only.
2. **Magic-link login** changes the auth model. Include it?
3. **Widget isolation.** Move the widget to an iframe with strict CSP (safer, a rewrite of `webchat_widget.py`) or keep the in-page widget?
4. **Payments scope.** Razorpay only (INR, GST) or also Stripe (USD)?
5. **Scope of phase 8.** Slack/Telegram now, or after launch?
6. **Start point.** Phase 1 first (recommended), since answer quality drives everything else.
