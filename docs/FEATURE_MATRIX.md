# Feature matrix: AI call-center platforms vs this backend

Compared against what the leading voice-agent platforms advertise (Vapi, Retell,
Bland, Synthflow; see sources) plus classic contact-center needs. Status was
checked by searching this repo's code, not by marketing claims.
Legend: YES built, PARTIAL exists with limits, NO not built.

| Area | Feature | Status |
|---|---|---|
| Voice | Inbound + outbound AI calls (Twilio, Sarvam STT/TTS/LLM) | YES |
| Voice | Barge-in / interruption handling (Go gateway) | YES |
| Voice | Multilingual (Indian languages via Sarvam) | YES |
| Voice | Call recording, transcripts, summaries, sentiment, intent, outcome | YES |
| Voice | Human warm transfer, IVR, queues, ring groups, park, voicemail | YES |
| Voice | SIP trunking | PARTIAL (schema/config present) |
| Voice | Answering-machine detection on outbound | NO |
| Voice | Streaming LLM -> TTS (lower first-word latency) | NO (per-sentence TTS only) |
| Voice | Voice cloning / custom voice upload | NO (provider voice selection only) |
| Campaigns | Batch outbound voice + WhatsApp campaigns, throttling, retries | YES |
| Campaigns | Calling windows / blocklist / spam protection | YES |
| Channels | WhatsApp, SMS, email, web chat widget, web forms, Meta (IG/Messenger) | YES |
| Agent | Tool/function calling, workflows, guardrails, flow + scenario testing | YES |
| Agent | Knowledge base (full-text search) | YES |
| Agent | Semantic/vector search, PDF/DOCX ingestion | NO |
| Ops | Tickets, SLA, appointments, follow-ups, promises-to-pay, CRM sync | YES |
| Ops | QA scoring, analytics, forecasting | YES |
| Ops | CSAT / post-call survey collection | NO |
| Ops | Live supervisor monitor / whisper | PARTIAL (permissions only; not verified end to end) |
| Platform | Multi-tenant, RBAC + custom roles, SSO (OIDC), TOTP 2FA, audit log | YES |
| Platform | PII masking, retention policy, API keys with scopes, rate limits, IP allowlist | YES |
| Platform | Billing (Razorpay), usage metering, outbound webhooks, SDKs (JS/PHP/Python) | YES |
| Platform | White-label branding / custom domains | NO |
| MCP | Streamable-HTTP MCP server, tenant-scoped keys | YES |
| MCP | Wide tool catalogue (22 tools), annotations, prompts, scopes, rate limit | YES (added) |
| MCP | OAuth 2.1 / dynamic client registration for OAuth-only hosts | NO |
| MCP | MCP resources (e.g. transcripts as resources) | NO |

Sources: [Vapi vs Retell vs Bland vs Synthflow](https://www.morphllm.com/comparisons/vapi-vs-retell-vs-bland-vs-synthflow),
[Retell comparison](https://www.retellai.com/blog/retell-vs-bland-vs-synthflow-vs-vapi),
[MCP authorization overview](https://mcp.directory/blog/oauth-21-for-remote-mcp-servers-streamable-http-explained-2026).
Vendor pages are marketing; the MCP auth source is a secondary blog, so check the
official MCP spec before building OAuth.

## Suggested next builds (highest value first)
1. OAuth 2.1 authorization server in front of `/mcp` (unlocks OAuth-only hosts).
2. Answering-machine detection (Twilio `AsyncAmd` + hang up / leave voicemail).
3. Streaming LLM -> TTS for latency.
4. CSAT survey after calls/tickets (WhatsApp/SMS).
5. pgvector semantic search + PDF ingestion.
