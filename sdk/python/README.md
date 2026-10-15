# AutoNXT Python SDK

Standard-library only (Python 3.9+).

```python
from autonxt import AutoNXT, construct_event

client = AutoNXT("ak_live_...", base_url="https://your-api-host/api/v1")

customer = client.customers.create(name="Jane", phone="+919800000000")
call = client.calls.create(direction="outbound", to_number=customer["phone"],
                           from_number="+14155550100", ai_agent_id="<agent id>")
calls = client.calls.list(limit=20, status="completed")
transcript = client.calls.transcript(call["id"])

overview = client.analytics.overview()
campaign = client.campaigns.create(name="Renewals", channel="voice", ai_agent_id="<agent id>")
client.campaigns.add_contacts(campaign["id"], [customer["id"]])
client.campaigns.launch(campaign["id"])

agents = client.ai_agents.list()
hook = client.webhooks.create(url="https://example.com/hook", events=["call.ended"])  # secret shown once
```

Errors raise `ApiError` (`status`, `detail`). `calls.create` sends an `Idempotency-Key`
(generated unless passed) so retries never dial twice.

## Verifying webhooks

```python
# Flask: raw = request.get_data()
event = construct_event(WEBHOOK_SECRET, request.headers, raw)  # raises SignatureVerificationError
```

Signature = hex(HMAC-SHA256(secret, f"{X-Timestamp}.{raw_body}")) in `X-Signature`; timestamps older
than 5 minutes are rejected. Tests: `PYTHONPATH=. pytest tests`.
