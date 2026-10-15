import pytest
from app.api.v1 import users as users_module
from app.core.config import get_settings
from httpx import AsyncClient


async def _owner_headers(client: AsyncClient, slug: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "company_name": slug.title(),
            "tenant_slug": slug,
            "full_name": "Owner",
            "email": f"owner@{slug}.com",
            "password": "supersecret1",
        },
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_invite_without_smtp_returns_temporary_password(client: AsyncClient) -> None:
    headers = await _owner_headers(client, "invite-nosmtp")
    resp = await client.post(
        "/api/v1/users/invite",
        headers=headers,
        json={"email": "a@invite-nosmtp.com", "full_name": "A", "role_name": "AGENT"},
    )
    assert resp.status_code == 201
    assert resp.json()["temporary_password"]
    assert resp.json()["email_sent"] is False


async def test_invite_with_smtp_emails_link_and_hides_password(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[dict] = []

    async def fake_send_email(**kwargs) -> None:
        sent.append(kwargs)

    monkeypatch.setattr(get_settings(), "smtp_host", "smtp.test")
    monkeypatch.setattr(users_module, "send_email", fake_send_email)

    headers = await _owner_headers(client, "invite-smtp")
    resp = await client.post(
        "/api/v1/users/invite",
        headers=headers,
        json={"email": "b@invite-smtp.com", "full_name": "B", "role_name": "AGENT"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["email_sent"] is True
    assert body["temporary_password"] is None
    assert sent[0]["to_email"] == "b@invite-smtp.com"
    assert "reset-password?token=" in sent[0]["html_body"]
