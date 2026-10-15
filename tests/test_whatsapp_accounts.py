from httpx import AsyncClient


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


async def test_connect_whatsapp_account_encrypts_token_successfully(client: AsyncClient) -> None:
    """Exercises app/core/crypto.py's Fernet encryption on a real request —
    this is the path that breaks if ENCRYPTION_KEY isn't a valid Fernet key."""
    tokens = await _signup(client, slug="salon", email="owner@salon.com")

    resp = await client.post(
        "/api/v1/whatsapp-accounts",
        headers=_auth(tokens),
        json={
            "phone_number_id": "106540352242922",
            "waba_id": "102290129340398",
            "display_phone_number": "+15550783881",
            "access_token": "EAAG_fake_test_token_value",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["phone_number_id"] == "106540352242922"
    assert "access_token" not in body  # never echo the secret back

    list_resp = await client.get("/api/v1/whatsapp-accounts", headers=_auth(tokens))
    assert len(list_resp.json()) == 1
