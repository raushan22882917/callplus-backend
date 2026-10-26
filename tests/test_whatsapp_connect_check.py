"""Connecting WhatsApp checks the IDs and token with Meta first and explains any problem."""

import httpx
from app.integrations import whatsapp as wa_integration
from httpx import AsyncClient

from tests.test_whatsapp_pipeline import _auth, _signup


def _fake_meta(monkeypatch, status: int, body: dict) -> None:
    real = httpx.AsyncClient

    def factory(*a, **k):
        return real(
            transport=httpx.MockTransport(lambda req: httpx.Response(status, json=body)),
            timeout=k.get("timeout"),
        )

    monkeypatch.setattr(wa_integration.httpx, "AsyncClient", factory)


async def test_matching_ids_pass(monkeypatch):
    _fake_meta(monkeypatch, 200, {"data": [{"id": "111"}]})
    assert await wa_integration.check_account_details("w1", "111", "tok") is None


async def test_phone_id_from_another_account_is_explained(monkeypatch):
    _fake_meta(monkeypatch, 200, {"data": [{"id": "999"}]})
    msg = await wa_integration.check_account_details("w1", "111", "tok")
    assert msg and "doesn't belong" in msg


async def test_unknown_business_id_is_explained(monkeypatch):
    _fake_meta(
        monkeypatch,
        400,
        {
            "error": {
                "code": 100,
                "message": "Unsupported get request. Object with ID 'w1' does not exist",
            }
        },
    )
    msg = await wa_integration.check_account_details("w1", "111", "tok")
    assert msg and "Business account ID" in msg and "Object with ID" not in msg


async def test_bad_token_is_explained(monkeypatch):
    _fake_meta(
        monkeypatch, 401, {"error": {"code": 190, "message": "Error validating access token"}}
    )
    msg = await wa_integration.check_account_details("w1", "111", "tok")
    assert msg and "token" in msg


async def test_meta_outage_does_not_block_connecting(monkeypatch):
    _fake_meta(monkeypatch, 503, {})
    assert await wa_integration.check_account_details("w1", "111", "tok") is None


async def test_endpoint_refuses_with_the_reason(client: AsyncClient, monkeypatch):
    async def bad(*_a, **_k):
        return "Use the WhatsApp Business Account ID."

    monkeypatch.setattr("app.api.v1.whatsapp.check_account_details", bad)
    tokens = await _signup(client, "checkco")
    resp = await client.post(
        "/api/v1/whatsapp-accounts",
        headers=_auth(tokens),
        json={
            "phone_number_id": "1",
            "waba_id": "2",
            "display_phone_number": "+919876543210",
            "access_token": "t",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "Use the WhatsApp Business Account ID."
