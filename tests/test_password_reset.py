from datetime import timedelta

from app.core.security import create_purpose_token, password_fingerprint
from httpx import AsyncClient
from sqlalchemy import select

from packages.database.models import User
from packages.database.session import session_scope


async def _signup(client: AsyncClient, *, slug: str, email: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "company_name": slug.title(),
            "tenant_slug": slug,
            "full_name": "Owner",
            "email": email,
            "password": "supersecret1",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _auth(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _load_user_by_email(email: str) -> User:
    async with session_scope() as db_session:
        return (await db_session.execute(select(User).where(User.email == email))).scalar_one()


async def test_forgot_password_is_generic_for_unknown_email(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/forgot-password", json={"email": "nobody@example.com"})
    assert resp.status_code == 200
    assert "reset link has been sent" in resp.json()["message"]


async def test_forgot_password_reset_password_then_login(client: AsyncClient) -> None:
    await _signup(client, slug="reset-co", email="owner@reset-co.com")

    forgot_resp = await client.post(
        "/api/v1/auth/forgot-password", json={"email": "owner@reset-co.com"}
    )
    assert forgot_resp.status_code == 200

    # No SMTP is configured in tests (conftest blanks all provider creds),
    # so the reset link isn't retrievable from the API response by design —
    # build a token the same way forgot-password does, to exercise reset.
    user = await _load_user_by_email("owner@reset-co.com")
    token = create_purpose_token(
        user_id=user.id,
        purpose="password_reset",
        expires_delta=timedelta(minutes=30),
        fingerprint=password_fingerprint(user.hashed_password),
    )

    bad_reset = await client.post(
        "/api/v1/auth/reset-password",
        json={"token": "not-a-real-token", "new_password": "newpass123"},
    )
    assert bad_reset.status_code == 400

    reset_resp = await client.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": "newpass123"}
    )
    assert reset_resp.status_code == 200, reset_resp.text
    # The link is single-use: the password hash changed, so it no longer matches.
    reused = await client.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": "another-pass1"}
    )
    assert reused.status_code == 400

    old_login = await client.post(
        "/api/v1/auth/login", json={"email": "owner@reset-co.com", "password": "supersecret1"}
    )
    assert old_login.status_code == 401

    new_login = await client.post(
        "/api/v1/auth/login", json={"email": "owner@reset-co.com", "password": "newpass123"}
    )
    assert new_login.status_code == 200


async def test_signup_does_not_ask_for_email_confirmation(client: AsyncClient) -> None:
    tokens = await _signup(client, slug="verify-co", email="owner@verify-co.com")
    user = await _load_user_by_email("owner@verify-co.com")
    assert user.email_verified_at is not None

    me = await client.get("/api/v1/users/me", headers=_auth(tokens))
    assert me.status_code == 200
    assert (await client.post("/api/v1/auth/verify-email", json={"token": "unused"})).status_code == 404
    assert (await client.post("/api/v1/auth/resend-verification", headers=_auth(tokens))).status_code == 404
