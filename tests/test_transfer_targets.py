import pytest
from app.api.v1 import internal as internal_module
from app.integrations.twilio_client import (
    TRANSFER_UNAVAILABLE_MESSAGE,
    build_transfer_twiml,
)
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import CallEvent
from tests.test_call_control import (
    _auth,
    _internal_headers,
    _make_in_progress_call,
    _signup,
)


def test_transfer_twiml_rings_all_numbers_then_apologises() -> None:
    twiml = build_transfer_twiml(["+911111111111", "+922222222222"])
    assert "<Number>+911111111111</Number>" in twiml
    assert "<Number>+922222222222</Number>" in twiml
    assert twiml.index("<Dial") < twiml.index("<Say>") < twiml.index("<Hangup")
    assert TRANSFER_UNAVAILABLE_MESSAGE in twiml


def test_transfer_twiml_without_numbers_just_hangs_up() -> None:
    twiml = build_transfer_twiml([])
    assert "<Hangup" in twiml and "<Dial" not in twiml


async def test_transfer_target_crud_and_validation(client: AsyncClient) -> None:
    tokens = await _signup(client, slug="tt-co", email="owner@ttco.com")
    bad = await client.post(
        "/api/v1/transfer-targets",
        headers=_auth(tokens),
        json={"name": "Rep", "phone_number": "12345"},
    )
    assert bad.status_code == 422

    created = await client.post(
        "/api/v1/transfer-targets",
        headers=_auth(tokens),
        json={"name": "Rep", "phone_number": "+919876543210"},
    )
    assert created.status_code == 201, created.text
    target_id = created.json()["id"]

    patched = await client.patch(
        f"/api/v1/transfer-targets/{target_id}",
        headers=_auth(tokens),
        json={"is_active": False},
    )
    assert patched.json()["is_active"] is False

    listed = await client.get("/api/v1/transfer-targets", headers=_auth(tokens))
    assert [t["id"] for t in listed.json()] == [target_id]

    deleted = await client.delete(f"/api/v1/transfer-targets/{target_id}", headers=_auth(tokens))
    assert deleted.status_code == 204


async def test_transfer_targets_are_tenant_isolated(client: AsyncClient) -> None:
    tokens_a = await _signup(client, slug="tt-a", email="owner@tta.com")
    tokens_b = await _signup(client, slug="tt-b", email="owner@ttb.com")
    created = await client.post(
        "/api/v1/transfer-targets",
        headers=_auth(tokens_a),
        json={"name": "Rep", "phone_number": "+919876543210"},
    )
    target_id = created.json()["id"]
    assert (await client.get("/api/v1/transfer-targets", headers=_auth(tokens_b))).json() == []
    resp = await client.patch(
        f"/api/v1/transfer-targets/{target_id}", headers=_auth(tokens_b), json={"name": "x"}
    )
    assert resp.status_code == 404


async def test_finalize_transfer_rings_active_targets_in_priority_order(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens = await _signup(client, slug="tt-xfer", email="owner@ttxfer.com")
    for name, phone, priority, active in [
        ("Second", "+912222222222", 20, True),
        ("First", "+911111111111", 10, True),
        ("Off", "+913333333333", 1, False),
    ]:
        resp = await client.post(
            "/api/v1/transfer-targets",
            headers=_auth(tokens),
            json={"name": name, "phone_number": phone, "priority": priority, "is_active": active},
        )
        assert resp.status_code == 201
    agent = await client.post("/api/v1/ai-agents", headers=_auth(tokens), json={"name": "Bot"})
    call = await _make_in_progress_call(
        db_session, tenant_slug="tt-xfer", agent_id=agent.json()["id"], started_seconds_ago=5
    )
    call.provider_call_sid = "CA123"
    await db_session.commit()

    redirected: dict = {}

    async def fake_credentials(db, tenant_id):
        return None

    def fake_redirect(*, call_sid, twiml, credentials):
        redirected.update(call_sid=call_sid, twiml=twiml)

    monkeypatch.setattr(internal_module, "get_twilio_credentials", fake_credentials)
    monkeypatch.setattr(internal_module, "redirect_call", fake_redirect)

    resp = await client.post(
        f"/api/v1/internal/calls/{call.id}/finalize",
        headers=_internal_headers(),
        json={"action": "transfer", "reason": "wants a human"},
    )
    assert resp.status_code == 200, resp.text
    twiml = redirected["twiml"]
    assert twiml.index("+911111111111") < twiml.index("+912222222222")
    assert "+913333333333" not in twiml

    events = (
        await db_session.execute(select(CallEvent).where(CallEvent.type == "TRANSFER_REQUESTED"))
    ).scalars().all()
    assert len(events) == 1 and events[0].payload["targets"] == 2
