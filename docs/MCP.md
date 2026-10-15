# Connecting AI clients to the call center over MCP

The platform exposes a remote MCP server (Streamable HTTP, stateless) at:

```
POST {PUBLIC_BASE_URL}/mcp/
Authorization: Bearer <api key>
```

Any MCP host that supports remote servers with a bearer header can connect:
Claude (Desktop / web / Code), Cursor, VS Code, custom agents built on the MCP
SDKs, and automation tools.

## Keys

| Key | How to get it | Access |
|---|---|---|
| MCP key `ccb_mcp_…` | `POST /api/v1/mcp/api-keys` (needs `mcp.manage`) | every tool for that tenant |
| Developer key `ak_live_…` / `ak_test_…` | `/api/v1/developer/api-keys` | only tools whose scope the key has |

Prefer a developer key with the narrowest scopes for each customer/integration.
Keys are tenant-scoped, rate-limited per key (`API_KEY_RATE_LIMIT_PER_MINUTE`),
stored hashed, and revocable. Tenant rows are also protected by Postgres RLS.
Write-scope tool calls are written to the audit log.

## Tools

| Tool | Scope | Kind |
|---|---|---|
| `list_calls`, `get_call`, `get_call_transcript`, `get_call_stats` | `calls:read` | read |
| `trigger_call` | `calls:write` | places a real call |
| `get_customer`, `search_customers`, `list_recent_tickets`, `list_tickets`, `check_availability` | `customers:read` | read |
| `create_customer`, `create_ticket`, `update_ticket_status`, `add_customer_note`, `schedule_callback`, `book_appointment` | `customers:write` | write |
| `send_whatsapp_message`, `create_customer_payment_link` | `messages:write` | contacts a customer |
| `list_ai_agents`, `search_knowledge_base` | `agents:read` | read |
| `list_phone_numbers` | `numbers:read` | read |
| `list_campaigns` | `campaigns:read` | read |

Every tool carries MCP annotations (`readOnlyHint`, `openWorldHint`) so hosts can
auto-approve reads and ask before anything that reaches a customer.

Prompts: `daily_call_review`, `customer_360`, `missed_call_followup`.

## Client config examples

Claude Code:
```bash
claude mcp add --transport http callcenter {PUBLIC_BASE_URL}/mcp/ \
  --header "Authorization: Bearer ccb_mcp_xxx"
```

Generic JSON (hosts that take a remote-server entry):
```json
{ "mcpServers": { "callcenter": {
  "url": "https://YOUR_HOST/mcp/",
  "headers": { "Authorization": "Bearer ccb_mcp_xxx" } } } }
```

## Known limits

- Auth is bearer-key only. Hosts that *require* OAuth 2.1 discovery
  (RFC 9728 / dynamic client registration) for remote servers, such as some
  hosted "connector" UIs, can't connect yet; that is the main roadmap item.
- Developer-key IP allowlists are not applied on the MCP path (no request object
  reaches the tool layer); the per-key rate limit and scopes are.
- Transcripts are returned unmasked to any key with `calls:read`.
