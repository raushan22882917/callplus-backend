import httpx
import pytest
from app.integrations import email_client


@pytest.mark.parametrize(
    "provider,host", [("resend", "api.resend.com"), ("brevo", "api.brevo.com")]
)
async def test_api_provider_posts_over_https(provider, host, monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(
        email_client.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw)
    )
    await email_client._send_via_api(provider, "k", "a@b.co", "c@d.co", "s", "<p>x</p>", {})
    assert seen["host"] == host


async def test_api_provider_rejection_raises(monkeypatch):
    transport = httpx.MockTransport(lambda r: httpx.Response(403))
    real = httpx.AsyncClient
    monkeypatch.setattr(
        email_client.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw)
    )
    with pytest.raises(RuntimeError):
        await email_client._send_via_api("resend", "k", "a@b.co", "c@d.co", "s", "x", {})
