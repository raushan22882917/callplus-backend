"""Ready-made automations: the owner picks one, answers a question, the real engine runs it."""

import uuid

import pytest
from app.workflows import recipes
from app.workflows.dispatch import run_pending_workflows
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import (
    CustomerActivity,
    Ticket,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowTrigger,
)
from tests.test_whatsapp_pipeline import _auth, _signup


async def _workspace(client: AsyncClient, slug: str) -> tuple[dict, uuid.UUID, str]:
    tokens = await _signup(client, slug)
    me = (await client.get("/api/v1/users/me", headers=_auth(tokens))).json()
    customer = await client.post(
        "/api/v1/customers", headers=_auth(tokens), json={"name": "Rahul", "phone": "+919800000001"}
    )
    assert customer.status_code == 201, customer.text
    return tokens, uuid.UUID(me["tenant_id"]), customer.json()["id"]


async def _tickets(db: AsyncSession, tenant_id: uuid.UUID) -> list[Ticket]:
    return list(
        (await db.execute(select(Ticket).where(Ticket.tenant_id == tenant_id))).scalars().all()
    )


def test_every_recipe_is_written_in_plain_words_and_has_safe_defaults():
    assert len(recipes.RECIPES) >= 4
    for r in recipes.RECIPES:
        assert r.title and r.when and r.then
        for banned in ("JSON", "payload", "webhook", "trigger", "API"):
            assert banned.lower() not in f"{r.title} {r.when} {r.then}".lower()
        definition = recipes.build(r, {})  # defaults alone must be a valid automation
        assert definition["recipe"] == r.id and definition["nodes"]
        for q in r.questions:
            assert q.default in {c.value for c in q.choices}


def test_a_wrong_answer_is_refused_in_plain_words():
    r = recipes.get_recipe("missed_call_callback")
    with pytest.raises(recipes.RecipeError, match="Please choose one"):
        recipes.build(r, {"when": "whenever"})
    with pytest.raises(recipes.RecipeError, match="don't have that"):
        recipes.get_recipe("nope")


async def test_the_catalogue_lists_the_recipes(client: AsyncClient):
    tokens, _, _ = await _workspace(client, "acme")
    resp = await client.get("/api/v1/workflows/recipes", headers=_auth(tokens))
    assert resp.status_code == 200, resp.text
    ids = {r["id"] for r in resp.json()}
    assert {
        "missed_call_callback",
        "failed_call_ticket",
        "new_customer_hello",
        "note_every_call",
    } <= ids
    first = resp.json()[0]
    assert set(first) >= {"id", "title", "when", "then", "trigger", "questions"}


async def test_the_catalogue_needs_a_signed_in_user(client: AsyncClient):
    assert (await client.get("/api/v1/workflows/recipes")).status_code in (401, 403)
    assert (
        await client.post("/api/v1/workflows/from-recipe", json={"recipe": "x"})
    ).status_code in (401, 403)


async def test_missed_call_recipe_asks_for_a_callback_only_when_nobody_answered(
    client: AsyncClient, db_session: AsyncSession
):
    tokens, tenant_id, customer_id = await _workspace(client, "acme")
    created = await client.post(
        "/api/v1/workflows/from-recipe",
        headers=_auth(tokens),
        json={"recipe": "missed_call_callback", "answers": {"when": "tomorrow morning"}},
    )
    assert created.status_code == 201, created.text
    assert created.json()["is_active"] is True and created.json()["trigger"] == "call_ended"

    answered = {"status": "completed", "customer_id": customer_id, "duration_seconds": 40}
    await run_pending_workflows(
        db_session, tenant_id=tenant_id, trigger=WorkflowTrigger.CALL_ENDED, payload=answered
    )
    assert await _tickets(db_session, tenant_id) == []

    missed = {"status": "no_answer", "customer_id": customer_id, "duration_seconds": 0}
    await run_pending_workflows(
        db_session, tenant_id=tenant_id, trigger=WorkflowTrigger.CALL_ENDED, payload=missed
    )
    tickets = await _tickets(db_session, tenant_id)
    assert len(tickets) == 1 and "tomorrow morning" in tickets[0].subject
    runs = (await db_session.execute(select(WorkflowRun))).scalars().all()
    assert all(r.status == WorkflowRunStatus.COMPLETED for r in runs)


async def test_failed_call_recipe_opens_a_ticket_with_the_chosen_importance(
    client: AsyncClient, db_session: AsyncSession
):
    tokens, tenant_id, customer_id = await _workspace(client, "acme")
    await client.post(
        "/api/v1/workflows/from-recipe",
        headers=_auth(tokens),
        json={"recipe": "failed_call_ticket", "answers": {"priority": "high"}},
    )
    payload = {"status": "failed", "customer_id": customer_id}
    await run_pending_workflows(
        db_session, tenant_id=tenant_id, trigger=WorkflowTrigger.CALL_ENDED, payload=payload
    )
    (ticket,) = await _tickets(db_session, tenant_id)
    assert ticket.priority.value == "high" and "could not connect" in ticket.subject


async def test_new_customer_recipe_creates_a_welcome_task(
    client: AsyncClient, db_session: AsyncSession
):
    tokens, tenant_id, customer_id = await _workspace(client, "acme")
    await client.post(
        "/api/v1/workflows/from-recipe",
        headers=_auth(tokens),
        json={"recipe": "new_customer_hello"},
    )
    await run_pending_workflows(
        db_session,
        tenant_id=tenant_id,
        trigger=WorkflowTrigger.CUSTOMER_CREATED,
        payload={"customer_id": customer_id, "phone": "+919800000001", "source": "call"},
    )
    (ticket,) = await _tickets(db_session, tenant_id)
    assert "say hello" in ticket.subject.lower() and ticket.priority.value == "low"


async def test_note_recipe_writes_on_the_customers_profile(
    client: AsyncClient, db_session: AsyncSession
):
    tokens, tenant_id, customer_id = await _workspace(client, "acme")
    await client.post(
        "/api/v1/workflows/from-recipe", headers=_auth(tokens), json={"recipe": "note_every_call"}
    )
    payload = {"status": "completed", "customer_id": customer_id, "duration_seconds": 12}
    await run_pending_workflows(
        db_session, tenant_id=tenant_id, trigger=WorkflowTrigger.CALL_ENDED, payload=payload
    )
    acts = (
        (
            await db_session.execute(
                select(CustomerActivity).where(CustomerActivity.tenant_id == tenant_id)
            )
        )
        .scalars()
        .all()
    )
    assert any("Open Calls for the summary" in str(a.__dict__) for a in acts)


async def test_the_same_automation_cannot_be_switched_on_twice(client: AsyncClient):
    tokens, _, _ = await _workspace(client, "acme")
    first = await client.post(
        "/api/v1/workflows/from-recipe", headers=_auth(tokens), json={"recipe": "note_every_call"}
    )
    again = await client.post(
        "/api/v1/workflows/from-recipe", headers=_auth(tokens), json={"recipe": "note_every_call"}
    )
    assert first.status_code == 201 and again.status_code == 409
    assert "already have" in again.json()["detail"]


async def test_a_bad_choice_or_unknown_recipe_is_a_plain_422(client: AsyncClient):
    tokens, _, _ = await _workspace(client, "acme")
    bad = await client.post(
        "/api/v1/workflows/from-recipe",
        headers=_auth(tokens),
        json={"recipe": "missed_call_callback", "answers": {"when": "never"}},
    )
    unknown = await client.post(
        "/api/v1/workflows/from-recipe", headers=_auth(tokens), json={"recipe": "nope"}
    )
    assert bad.status_code == 422 and "Please choose" in bad.json()["detail"]
    assert unknown.status_code == 422


async def test_each_workspace_has_its_own_automations(client: AsyncClient):
    a, _, _ = await _workspace(client, "alpha")
    b, _, _ = await _workspace(client, "beta")
    await client.post(
        "/api/v1/workflows/from-recipe", headers=_auth(a), json={"recipe": "note_every_call"}
    )
    listed_b = await client.get("/api/v1/workflows", headers=_auth(b))
    assert listed_b.json() == []
    again_b = await client.post(
        "/api/v1/workflows/from-recipe", headers=_auth(b), json={"recipe": "note_every_call"}
    )
    assert again_b.status_code == 201
