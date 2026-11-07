"""Knowledge retrieval: the built-in embedder, synonyms, hybrid search with
rank fusion and the quality gate, file extraction, and citations in chat."""

import io
import json
import math
import uuid
import zipfile

import pytest
from app.services import embeddings, knowledge
from app.services.knowledge_extract import ExtractError, extract_text, html_to_text
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import KnowledgeChunk, KnowledgeDocument, Tenant
from packages.database.session import session_scope
from packages.database.vector_support import EMBEDDING_DIM, ensure_embedding_column

TEXT = {"Content-Type": "text/plain", "Origin": "https://shop.test"}


async def _signup(client: AsyncClient, slug: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"company_name": slug.title(), "tenant_slug": slug, "full_name": "Owner",
              "email": f"o@{slug}.com", "password": "supersecret1"},
    )
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _add(client, auth, title: str, content: str) -> dict:
    resp = await client.post("/api/v1/knowledge/documents", headers=auth, json={"title": title, "content": content})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _search(client, auth, query: str, limit: int = 5) -> list[dict]:
    resp = await client.post("/api/v1/knowledge/search", headers=auth, json={"query": query, "limit": limit})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


# ---- the embedder and the words ------------------------------------------------------------


def test_embeddings_are_fixed_size_unit_length_and_repeatable():
    e = embeddings.get_embedder()
    one, again, empty = e.embed(["How do I get a refund?"])[0], e.embed(["How do I get a refund?"])[0], e.embed([""])[0]
    assert len(one) == EMBEDDING_DIM == e.dim and one == again
    assert math.isclose(math.sqrt(sum(v * v for v in one)), 1.0, rel_tol=1e-9)
    assert empty == [0.0] * EMBEDDING_DIM  # nothing to embed is not a crash


def test_similar_texts_sit_closer_than_unrelated_ones():
    e = embeddings.get_embedder()
    q, close, far = e.embed(["refund policy for returned items", "Our refund policy covers returned items within 30 days.",
                             "The salon opens at ten every morning."])
    assert _cosine(q, close) > 0.3 > _cosine(q, far)


def test_terms_are_stemmed_content_words_and_synonyms_join_up():
    assert embeddings.terms("What are the opening hours of the shop?") == ["open", "hour", "shop"]
    assert embeddings.stem("refunds") == embeddings.stem("refund")
    assert embeddings.stem("is") == "is" and embeddings.stem("gas") == "gas"  # too short to strip
    assert embeddings.synonyms_of("cost") >= {"price", "cost", "fee"}
    assert "refund" in embeddings.expand("how do I return it")
    assert embeddings.synonyms_of("zebra") == frozenset({"zebra"})
    assert "price" in embeddings.terms("कीमत क्या है")


# ---- search ---------------------------------------------------------------------------------


async def test_a_keyword_style_question_finds_the_passage_that_uses_other_words(client: AsyncClient):
    auth = await _signup(client, "syn-co")
    await _add(client, auth, "Pricing", "Our plans start at 999 rupees a month, billed yearly.")
    await _add(client, auth, "Hours", "We are open from ten to eight, Monday to Saturday.")
    hits = await _search(client, auth, "what does it cost")
    assert hits and hits[0]["title"] == "Pricing"
    assert [h["title"] for h in await _search(client, auth, "timings")] == ["Hours"]


async def test_results_say_where_they_came_from_and_are_ranked_by_fusion(client: AsyncClient):
    auth = await _signup(client, "fuse-co")
    await _add(client, auth, "Refunds", "Refunds are issued within seven days of the returned item arriving.")
    await _add(client, auth, "Shipping", "Shipping takes five business days across India.")
    hits = await _search(client, auth, "refund for a returned item")
    top = hits[0]
    assert top["title"] == "Refunds" and top["via"] == "both" and top["match"] >= 0.5
    assert top["chunk_id"] and top["chunk_index"] == 0 and 0 < top["score"] <= 1
    assert all(h["title"] != "Shipping" for h in hits)


async def test_the_quality_gate_drops_passages_that_only_share_a_stray_word(client: AsyncClient):
    auth = await _signup(client, "gate-co")
    await _add(client, auth, "Parking", "Free parking is available behind the building for all customers.")
    # one incidental word ("customers") out of a long question is not an answer
    assert await _search(client, auth, "can customers pay my invoice in euros using a wire transfer") == []
    # but a short question with a real overlap still matches
    assert (await _search(client, auth, "parking"))[0]["title"] == "Parking"
    assert await _search(client, auth, "the of and") == []  # nothing but filler words


async def test_search_is_tenant_scoped_and_respects_the_ai_switch(client: AsyncClient):
    a, b = await _signup(client, "iso-a"), await _signup(client, "iso-b")
    doc = await _add(client, a, "Secret", "The refund code word is pineapple.")
    assert await _search(client, b, "refund code") == []
    assert len(await _search(client, a, "refund code")) == 1
    async with session_scope() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.slug == "iso-a"))).scalar_one()
        from packages.database.models.guardrails import KnowledgeAIControl
        db.add(KnowledgeAIControl(tenant_id=tenant.id, document_id=uuid.UUID(doc["id"]), enabled=False))
        await db.commit()
    assert await _search(client, a, "refund code") == []


async def test_search_still_works_without_vectors(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(knowledge, "_vector_ready", False)

    async def no_vectors(db):
        return False

    monkeypatch.setattr(knowledge, "vector_enabled", no_vectors)
    auth = await _signup(client, "novec-co")
    await _add(client, auth, "Refunds", "Refunds are issued within seven days.")
    hit = (await _search(client, auth, "refund"))[0]
    assert hit["title"] == "Refunds" and hit["via"] == "text"


async def test_a_document_is_indexed_with_embeddings_and_reindex_covers_older_ones(client: AsyncClient, db_session: AsyncSession):
    auth = await _signup(client, "emb-co")
    doc = await _add(client, auth, "Hours", "Open every day from ten to eight.")
    assert doc["status"] == "ready" and doc["error"] is None and doc["chunk_count"] == 1
    count = (await db_session.execute(
        __import__("sqlalchemy").text("SELECT count(*) FROM knowledge_chunks WHERE embedding IS NOT NULL"))).scalar_one()
    assert count == 1
    await db_session.execute(__import__("sqlalchemy").text("UPDATE knowledge_chunks SET embedding = NULL"))
    await db_session.commit()
    resp = await client.post("/api/v1/knowledge/reindex", headers=auth)
    assert resp.status_code == 200 and resp.json() == {"passages_embedded": 1, "vector_search": True}
    assert (await client.post("/api/v1/knowledge/reindex")).status_code == 401


async def test_a_failing_embedder_leaves_the_document_searchable_by_keywords(client: AsyncClient, monkeypatch):
    class Broken:
        name, dim = "broken", EMBEDDING_DIM

        def embed(self, texts):
            raise RuntimeError("embedder down")

    monkeypatch.setattr(embeddings, "_embedder", Broken())
    auth = await _signup(client, "broken-co")
    doc = await _add(client, auth, "Hours", "Open every day from ten to eight.")
    assert doc["status"] == "ready" and "keyword search only" in doc["error"]
    assert (await _search(client, auth, "open hours"))[0]["title"] == "Hours"


async def test_adding_the_embedding_column_is_repeatable():
    from packages.database.session import get_engine

    async with get_engine().begin() as conn:
        assert await conn.run_sync(ensure_embedding_column) is True
        assert await conn.run_sync(ensure_embedding_column) is True


# ---- file extraction ------------------------------------------------------------------------


def _docx(*paragraphs: str) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = ('<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f"<w:body>{body}</w:body></w:document>")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("word/document.xml", xml)
    return out.getvalue()


def _pdf(text: str | None) -> bytes:
    """A one-page PDF; text=None gives a blank page like a scan with no text layer."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET" if text else ""
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


def test_html_loses_scripts_and_styles_and_keeps_the_words():
    text = html_to_text("<html><head><title>x</title><style>p{}</style></head><body><h1>Hours</h1>"
                        "<script>alert(1)</script><p>Open&nbsp;daily &amp; late.</p><ul><li>Mon</li><li>Tue</li></ul></body></html>")
    assert "alert" not in text and "p{}" not in text
    assert "Hours" in text and "Open daily & late." in text and "Mon" in text


def test_each_file_type_gives_its_text():
    assert extract_text("a.md", b"# Title\n\nBody") == "# Title\n\nBody"
    assert extract_text("a.csv", "name,price\nTea,10".encode()) .startswith("name,price")
    assert extract_text("a.docx", _docx("Refund policy", "Thirty days.")) == "Refund policy\n\nThirty days."
    assert "Refund within thirty days" in extract_text("a.pdf", _pdf("Refund within thirty days"))
    assert extract_text("A.HTML", b"<p>Hi</p>") == "Hi"


@pytest.mark.parametrize(
    "name, raw, message",
    [
        ("a.exe", b"x", "Unsupported file type"),
        ("a.txt", b"  \n ", "File is empty"),
        ("a.txt", b"\xff\xfe\x00bad", "not UTF-8"),
        ("a.docx", b"not a zip", "not a valid .docx"),
        ("a.pdf", b"not a pdf", "not a readable PDF"),
        ("a.pdf", _pdf(None), "no text in it"),
        ("a.txt", b"x" * 10_000_001, "exceeds 10 MB"),
    ],
)
def test_bad_files_are_refused_with_a_reason(name, raw, message):
    with pytest.raises(ExtractError, match=message):
        extract_text(name, raw)


async def test_uploading_files_through_the_api(client: AsyncClient):
    auth = await _signup(client, "up-co")

    async def upload(name: str, raw: bytes):
        return await client.post("/api/v1/knowledge/documents/upload", headers=auth, files={"file": (name, raw)})

    ok = await upload("faq.docx", _docx("We deliver in three days."))
    assert ok.status_code == 201 and ok.json()["source"] == "upload" and ok.json()["status"] == "ready"
    assert (await upload("policy.pdf", _pdf("Warranty lasts two years")) ).status_code == 201
    assert (await upload("page.html", b"<p>Visit us in Pune</p><script>x()</script>")).status_code == 201
    bad = await upload("scan.pdf", _pdf(None))
    assert bad.status_code == 400 and "scan" in bad.json()["detail"]
    assert (await upload("x.zip", b"x")).status_code == 400
    assert [h["title"] for h in await _search(client, auth, "warranty")] == ["policy.pdf"]
    assert [h["title"] for h in await _search(client, auth, "Pune")] == ["page.html"]


# ---- citations in website chat --------------------------------------------------------------


async def _chat(client: AsyncClient, auth: dict, **config) -> str:
    agent = await client.post("/api/v1/ai-agents", headers=auth, json={"name": f"Bot {uuid.uuid4().hex[:6]}"})
    resp = await client.put(
        "/api/v1/channels/settings/web_chat", headers=auth,
        json={"enabled": True, "ai_agent_id": agent.json()["id"], "config": {"privacy_consent": False, **config}},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["public_key"]


async def test_a_chat_answer_carries_the_documents_it_drew_on(client: AsyncClient, monkeypatch):
    async def fake_chat(*, messages, tools=None, api_key=None, **kw):
        return {"choices": [{"message": {"role": "assistant", "content": "Refunds take seven days."}}]}

    monkeypatch.setattr("app.services.agent_engine.chat_completion", fake_chat)
    auth = await _signup(client, "cite-co")
    await _add(client, auth, "Refund policy", "Refunds are issued within seven days of the returned item arriving.")
    key = await _chat(client, auth)
    base = f"/api/v1/channels/webchat/{key}"
    token = (await client.post(f"{base}/session", content="{}", headers=TEXT)).json()["session_token"]

    async def ask(text: str) -> dict:
        resp = await client.post(f"{base}/message", content=json.dumps({"session_token": token, "text": text}), headers=TEXT)
        assert resp.status_code == 200, resp.text
        return resp.json()["messages"][-1]

    answered = await ask("How long does a refund take?")
    assert answered["text"] == "Refunds take seven days." and answered["sources"] == [{"title": "Refund policy"}]
    unrelated = await ask("Do you sell bicycles?")
    assert unrelated["sources"] == []
    async with session_scope() as db:
        from packages.database.models import Message
        stored = (await db.execute(select(Message).where(Message.sources.is_not(None)))).scalars().all()
        assert len(stored) == 1 and stored[0].sources[0]["title"] == "Refund policy" and stored[0].sources[0]["chunk_id"]
    _ = (KnowledgeChunk, KnowledgeDocument)
