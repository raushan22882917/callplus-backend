# AutoNXT JavaScript SDK

Dependency-free client (Node 18+, uses `fetch` and `node:crypto`).

```js
import { AutoNXT, constructEvent } from "autonxt";

const client = new AutoNXT(process.env.AUTONXT_API_KEY, {
  baseUrl: "https://your-api-host/api/v1",
});

const customer = await client.customers.create({ name: "Jane", phone: "+919800000000" });
const call = await client.calls.create({
  direction: "outbound", to_number: customer.phone, from_number: "+14155550100", ai_agent_id: "<agent id>",
});
const calls = await client.calls.list({ limit: 20, status: "completed" });
const transcript = await client.calls.transcript(call.id);

const overview = await client.analytics.overview();
const campaign = await client.campaigns.create({ name: "Renewals", channel: "voice", ai_agent_id: "<agent id>" });
await client.campaigns.addContacts(campaign.id, [customer.id]);
await client.campaigns.launch(campaign.id);

const agents = await client.aiAgents.list();
const hook = await client.webhooks.create({ url: "https://example.com/hook", events: ["call.ended"] }); // secret shown once
```

Errors throw `ApiError` (`status`, `detail`). `calls.create` sends an `Idempotency-Key` (generated unless you pass
`{ idempotencyKey }`), so retries never dial twice.

## Verifying webhooks

```js
// Express: use express.raw({ type: "application/json" }) so req.body is the raw Buffer.
app.post("/hook", express.raw({ type: "application/json" }), (req, res) => {
  try {
    const event = constructEvent(process.env.WEBHOOK_SECRET, req.headers, req.body); // 5 min tolerance
    res.sendStatus(200);
  } catch { res.sendStatus(400); }
});
```

Signature = hex(HMAC-SHA256(secret, `${X-Timestamp}.${rawBody}`)) from the `X-Signature` header.
Tests: `npm test`.
