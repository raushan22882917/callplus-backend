import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from autonxt import ApiError, AutoNXT, SignatureVerificationError, construct_event, verify_signature


class _Handler(BaseHTTPRequestHandler):
    seen: list = []

    def _reply(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        _Handler.seen.append((self.command, self.path, dict(self.headers), None))
        if self.path.startswith("/api/v1/customers/missing"):
            return self._reply(404, {"detail": "Customer not found"})
        self._reply(200, [{"id": "1"}])

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _Handler.seen.append((self.command, self.path, dict(self.headers), json.loads(body)))
        self._reply(201, {"id": "new"})

    def log_message(self, *args):
        pass


@pytest.fixture()
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _Handler.seen = []
    yield f"http://127.0.0.1:{httpd.server_port}/api/v1"
    httpd.shutdown()


def test_requests_carry_auth_params_and_idempotency(server):
    client = AutoNXT("ak_test_x", base_url=server)
    assert client.customers.list(limit=5) == [{"id": "1"}]
    method, path, headers, _ = _Handler.seen[0]
    assert (method, path) == ("GET", "/api/v1/customers?limit=5&offset=0")
    assert headers["Authorization"] == "Bearer ak_test_x"

    client.calls.create(direction="outbound", to_number="+1", from_number="+2", ai_agent_id="a")
    _, path, headers, body = _Handler.seen[1]
    assert path == "/api/v1/calls" and body["direction"] == "outbound"
    assert headers["Idempotency-Key"]

    client.campaigns.add_contacts("c1", ["u1"])
    assert _Handler.seen[2][1] == "/api/v1/campaigns/c1/contacts"
    client.analytics.calls(from_date="2026-01-01")
    assert _Handler.seen[3][1] == "/api/v1/analytics/calls?from_date=2026-01-01"
    client.webhooks.create(url="https://x.io/h", events=["call.ended"])
    assert _Handler.seen[4][3] == {"url": "https://x.io/h", "events": ["call.ended"]}


def test_api_error(server):
    client = AutoNXT("ak_test_x", base_url=server)
    with pytest.raises(ApiError) as err:
        client.customers.get("missing")
    assert err.value.status == 404 and err.value.detail == "Customer not found"


def _sign(secret, ts, body):
    return hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()


def test_webhook_signature():
    body = '{"event":"call.ended"}'
    ts = int(time.time())
    sig = _sign("whsec_x", ts, body)
    assert verify_signature("whsec_x", ts, body, sig)
    assert not verify_signature("whsec_x", ts, body + " ", sig)
    assert not verify_signature("other", ts, body, sig)
    assert not verify_signature("whsec_x", ts - 1000, body, _sign("whsec_x", ts - 1000, body))
    event = construct_event("whsec_x", {"X-Timestamp": str(ts), "X-Signature": sig}, body)
    assert event["event"] == "call.ended"
    with pytest.raises(SignatureVerificationError):
        construct_event("whsec_x", {"X-Timestamp": str(ts), "X-Signature": "bad"}, body)
