import uuid

from app.services.knowledge import CHUNK_TARGET_CHARS, chunk_text
from app.tools.context import ToolContext
from app.tools.handlers import search_knowledge_base
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


async def _signup(client: AsyncClient, *, slug: str) -> dict:
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
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_chunk_text_splits_long_text_within_target() -> None:
    text = "\n\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(10))
    chunks = chunk_text(text)
    assert len(chunks) > 1
    assert all(len(c) <= CHUNK_TARGET_CHARS for c in chunks)
    assert chunk_text("   \n\n ") == []


def test_chunk_text_hard_splits_oversized_paragraph() -> None:
    chunks = chunk_text("x" * (CHUNK_TARGET_CHARS * 3))
    assert len(chunks) >= 3
    assert all(len(c) <= CHUNK_TARGET_CHARS for c in chunks)


async def test_create_search_and_delete_document(client: AsyncClient) -> None:
    headers = await _signup(client, slug="kb-co")
    resp = await client.post(
        "/api/v1/knowledge/documents",
        headers=headers,
        json={
            "title": "Refund policy",
            "content": "Customers may request a refund within 30 days of purchase.\n\n"
            "Shipping takes five business days.",
        },
    )
    assert resp.status_code == 201, resp.text
    doc = resp.json()
    assert doc["chunk_count"] >= 1

    hits = await client.post(
        "/api/v1/knowledge/search", headers=headers, json={"query": "refund window"}
    )
    assert hits.status_code == 200
    assert hits.json()[0]["title"] == "Refund policy"

    miss = await client.post(
        "/api/v1/knowledge/search", headers=headers, json={"query": "zeppelin"}
    )
    assert miss.json() == []

    deleted = await client.delete(f"/api/v1/knowledge/documents/{doc['id']}", headers=headers)
    assert deleted.status_code == 204
    after = await client.post(
        "/api/v1/knowledge/search", headers=headers, json={"query": "refund"}
    )
    assert after.json() == []


async def test_upload_rejects_bad_type_and_accepts_text(client: AsyncClient) -> None:
    headers = await _signup(client, slug="kb-upload-co")
    bad = await client.post(
        "/api/v1/knowledge/documents/upload",
        headers=headers,
        files={"file": ("x.exe", b"MZ", "application/octet-stream")},
    )
    assert bad.status_code == 400

    ok = await client.post(
        "/api/v1/knowledge/documents/upload",
        headers=headers,
        files={"file": ("faq.md", b"# FAQ\n\nWe open at nine am.", "text/markdown")},
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["source"] == "upload"


async def test_knowledge_is_tenant_isolated(client: AsyncClient, db_session: AsyncSession) -> None:
    headers_a = await _signup(client, slug="kb-a")
    headers_b = await _signup(client, slug="kb-b")
    created = await client.post(
        "/api/v1/knowledge/documents",
        headers=headers_a,
        json={"title": "Secret", "content": "The vault code is swordfish."},
    )
    doc_id = created.json()["id"]

    hits = await client.post(
        "/api/v1/knowledge/search", headers=headers_b, json={"query": "swordfish"}
    )
    assert hits.json() == []
    url = f"/api/v1/knowledge/documents/{doc_id}"
    assert (await client.get(url, headers=headers_b)).status_code == 404
    assert (await client.delete(url, headers=headers_b)).status_code == 404

    me = await client.get("/api/v1/users/me", headers=headers_a)
    ctx = ToolContext(
        db=db_session,
        tenant_id=uuid.UUID(me.json()["tenant_id"]),
        agent_id=None,
        call_id=None,
        conversation_id=None,
        customer_id=None,
    )
    result = await search_knowledge_base(ctx, query="vault code")
    assert result["found"] is True
