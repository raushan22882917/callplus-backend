import json as json_lib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any


class ApiError(Exception):
    def __init__(self, status: int, detail: Any):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class _Resource:
    def __init__(self, client: "AutoNXT"):
        self._c = client


class Calls(_Resource):
    def list(self, **filters: Any) -> list[dict]:
        """filters: limit, offset, ai_agent_id, direction, status, sentiment, outcome, q"""
        return self._c.request("GET", "/calls", params=filters)

    def get(self, call_id: str) -> dict:
        return self._c.request("GET", f"/calls/{call_id}")

    def create(self, *, idempotency_key: str | None = None, **body: Any) -> dict:
        """Place an outbound call: direction='outbound', to_number, from_number, ai_agent_id.
        A retry with the same idempotency key never dials twice (one is generated if omitted)."""
        key = idempotency_key or str(uuid.uuid4())
        return self._c.request("POST", "/calls", json=body, headers={"Idempotency-Key": key})

    def transcript(self, call_id: str) -> list[dict]:
        return self._c.request("GET", f"/calls/{call_id}/transcript")


class Customers(_Resource):
    def list(self, *, limit: int = 50, offset: int = 0) -> list[dict]:
        return self._c.request("GET", "/customers", params={"limit": limit, "offset": offset})

    def get(self, customer_id: str) -> dict:
        return self._c.request("GET", f"/customers/{customer_id}")

    def create(self, **body: Any) -> dict:
        """name (required), phone, email, tags"""
        return self._c.request("POST", "/customers", json=body)

    def update(self, customer_id: str, **body: Any) -> dict:
        return self._c.request("PATCH", f"/customers/{customer_id}", json=body)

    def delete(self, customer_id: str) -> None:
        self._c.request("DELETE", f"/customers/{customer_id}")


class Campaigns(_Resource):
    def list(self) -> list[dict]:
        return self._c.request("GET", "/campaigns")

    def get(self, campaign_id: str) -> dict:
        return self._c.request("GET", f"/campaigns/{campaign_id}")

    def create(self, **body: Any) -> dict:
        """name, channel ('voice'|'whatsapp'), ai_agent_id, whatsapp_template_name, scheduled_at"""
        return self._c.request("POST", "/campaigns", json=body)

    def add_contacts(self, campaign_id: str, customer_ids: list[str]) -> list[dict]:
        return self._c.request(
            "POST", f"/campaigns/{campaign_id}/contacts", json={"customer_ids": customer_ids}
        )

    def contacts(self, campaign_id: str) -> list[dict]:
        return self._c.request("GET", f"/campaigns/{campaign_id}/contacts")

    def launch(self, campaign_id: str) -> dict:
        return self._c.request("POST", f"/campaigns/{campaign_id}/launch")

    def pause(self, campaign_id: str) -> dict:
        return self._c.request("POST", f"/campaigns/{campaign_id}/pause")

    def resume(self, campaign_id: str) -> dict:
        return self._c.request("POST", f"/campaigns/{campaign_id}/resume")


class Analytics(_Resource):
    def overview(self) -> dict:
        return self._c.request("GET", "/analytics/overview")

    def calls(self, *, from_date: str | None = None, to_date: str | None = None) -> dict:
        return self._c.request(
            "GET", "/analytics/calls", params={"from_date": from_date, "to_date": to_date}
        )

    def tickets(self, **params: Any) -> dict:
        return self._c.request("GET", "/analytics/tickets", params=params)

    def campaigns(self, **params: Any) -> dict:
        return self._c.request("GET", "/analytics/campaigns", params=params)


class AiAgents(_Resource):
    def list(self) -> list[dict]:
        return self._c.request("GET", "/ai-agents")

    def get(self, agent_id: str) -> dict:
        return self._c.request("GET", f"/ai-agents/{agent_id}")

    def create(self, **body: Any) -> dict:
        return self._c.request("POST", "/ai-agents", json=body)

    def update(self, agent_id: str, **body: Any) -> dict:
        return self._c.request("PATCH", f"/ai-agents/{agent_id}", json=body)


class Webhooks(_Resource):
    def list(self) -> list[dict]:
        return self._c.request("GET", "/developer/webhooks")

    def create(self, *, url: str, events: list[str], **extra: Any) -> dict:
        """The signing secret is returned once, in this response."""
        return self._c.request(
            "POST", "/developer/webhooks", json={"url": url, "events": events, **extra}
        )

    def update(self, endpoint_id: str, **body: Any) -> dict:
        return self._c.request("PATCH", f"/developer/webhooks/{endpoint_id}", json=body)

    def delete(self, endpoint_id: str) -> None:
        self._c.request("DELETE", f"/developer/webhooks/{endpoint_id}")

    def test(self, endpoint_id: str) -> dict:
        return self._c.request("POST", f"/developer/webhooks/{endpoint_id}/test")

    def deliveries(self, limit: int = 50) -> list[dict]:
        return self._c.request("GET", "/developer/webhooks/deliveries", params={"limit": limit})


class AutoNXT:
    """Client for the AutoNXT public API.

        client = AutoNXT("ak_live_...", base_url="https://api.example.com/api/v1")
        client.customers.create(name="Jane", phone="+919800000000")
    """

    def __init__(
        self, api_key: str, *, base_url: str = "http://localhost:8000/api/v1", timeout: float = 30.0
    ):
        if not api_key:
            raise ValueError("api_key is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.calls = Calls(self)
        self.customers = Customers(self)
        self.campaigns = Campaigns(self)
        self.analytics = Analytics(self)
        self.ai_agents = AiAgents(self)
        self.webhooks = Webhooks(self)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        url = self.base_url + path
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            if query:
                url += "?" + query
        data = None if json is None else json_lib.dumps(json).encode()
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "autonxt-python/0.1.0")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:  # noqa: S310
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                detail = json_lib.loads(raw).get("detail", raw.decode())
            except ValueError:
                detail = raw.decode(errors="replace")
            raise ApiError(exc.code, detail) from None
        return json_lib.loads(raw) if raw else None
