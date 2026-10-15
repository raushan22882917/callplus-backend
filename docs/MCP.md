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

## OAuth 2.1 (for hosts that require it)

Hosts that only support OAuth connect with just the URL `{PUBLIC_BASE_URL}/mcp/`.
An unauthenticated request gets `401` + `WWW-Authenticate: Bearer resource_metadata=...`
(RFC 9728), and the host then discovers and uses:

| Endpoint | Purpose |
|---|---|
| `/.well-known/oauth-protected-resource` | resource metadata (RFC 9728) |
| `/.well-known/oauth-authorization-server` | server metadata (RFC 8414) |
| `POST /oauth/register` | dynamic client registration (RFC 7591, public clients) |
| `GET/POST /oauth/authorize` | sign-in + scope consent page |
| `POST /oauth/token` | `authorization_code` (PKCE S256 only) and `refresh_token` |
| `POST /oauth/revoke` | token revocation (RFC 7009) |

Details: the approving user signs in with their normal credentials (and 2FA if
enabled), needs the `mcp.manage` permission, and ticks which scopes to grant (only
scopes their role allows are honoured). Access tokens last 1 hour; refresh tokens
30 days and rotate on use, and replaying a rotated refresh token revokes the whole
family. Codes are single-use (10 min). Tokens are stored hashed. Redirect URIs must
be https, loopback http, or an app scheme. Set `PUBLIC_BASE_URL` to the real
public **https** origin, since it is the OAuth issuer.

## Tools

| Tool | Scope | Kind |
|---|---|---|
| `list_calls`, `get_call`, `get_call_transcript`, `get_call_stats`, `get_csat_summary` | `calls:read` | read |
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

- Only public OAuth clients (no client secret) are supported.
- OAuth was tested end to end against this server's own endpoints, not yet
  against a live third-party host; try it with the MCP Inspector or your host.
- Developer-key IP allowlists are not applied on the MCP path (no request object
  reaches the tool layer); the per-key rate limit and scopes are.
- Transcripts are returned unmasked to any key with `calls:read`.
