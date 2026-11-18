"""The signature a carrier stream carries so the realtime gateway can tell a real call from a stranger."""

import hashlib
import hmac
from types import SimpleNamespace

from app.core.config import get_settings
from app.integrations import twilio_client
from app.services import ivr, stream_auth

CALL_ID = "11111111-2222-3333-4444-555555555555"


def test_signature_matches_the_gateway_vector(monkeypatch):
    # The same vector is pinned in apps/realtime/internal/session/streamauth_test.go.
    monkeypatch.setattr(
        stream_auth, "get_settings", lambda: SimpleNamespace(internal_service_token="unit-test-internal-token-123456")
    )
    params = stream_auth.stream_params(CALL_ID, now=1700000000)
    assert params == {
        "exp": "1700000600",
        "sig": "77c12312149002b2a1419be4d8875dcb56e85797680f36e1d51926c99eb28c7e",
    }


def test_signature_is_bound_to_the_call():
    assert stream_auth.stream_params(CALL_ID, now=1)["sig"] != stream_auth.stream_params("other", now=1)["sig"]


def _expected_sig(exp: str) -> str:
    key = get_settings().internal_service_token.encode()
    return hmac.new(key, f"stream:{CALL_ID}:{exp}".encode(), hashlib.sha256).hexdigest()


def _params(twiml: str) -> dict[str, str]:
    import xml.etree.ElementTree as ET

    return {p.attrib["name"]: p.attrib["value"] for p in ET.fromstring(twiml).iter("Parameter")}


def test_twilio_stream_twiml_is_signed():
    params = _params(twilio_client.build_stream_twiml(call_id=CALL_ID, tenant_id="t1"))
    assert params["callId"] == CALL_ID
    assert params["sig"] == _expected_sig(params["exp"])


def test_ivr_stream_twiml_is_signed():
    params = _params(ivr.build_stream_twiml(stream_url="wss://rt.example.com/ws/twilio", call_id=CALL_ID, tenant_id="t1"))
    assert params["sig"] == _expected_sig(params["exp"])
