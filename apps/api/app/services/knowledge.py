"""Knowledge base: chunk tenant documents and find the passages that answer a
question. Retrieval is hybrid: Postgres full-text search and (when the database
has pgvector) cosine similarity over embeddings, merged by reciprocal-rank
fusion, then held to a lexical-overlap quality gate so a vector neighbour that
shares no real words with the question is not offered as an answer."""

import logging
import re
import uuid

from app.services import embeddings
from sqlalchemy import cast, delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import KnowledgeChunk, KnowledgeDocument
from packages.database.models.guardrails import KnowledgeAIControl
from packages.database.vector_support import Vector, chunk_vectors, vector_literal

CHUNK_TARGET_CHARS = 800
CHUNK_OVERLAP_CHARS = 100
CANDIDATES = 30  # per ranking, before fusion
RRF_K = 60
MIN_TERM_MATCH = 0.25  # of a longer question's words, a passage must cover this share

logger = logging.getLogger(__name__)
_vector_ready = False


def chunk_text(text: str) -> list[str]:
    """Split on paragraph boundaries into ~CHUNK_TARGET_CHARS pieces, hard-
    splitting any single paragraph that is longer, with a small overlap so a
    sentence cut at a boundary still appears whole in one chunk."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        while len(paragraph) > CHUNK_TARGET_CHARS:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(paragraph[:CHUNK_TARGET_CHARS])
            paragraph = paragraph[CHUNK_TARGET_CHARS - CHUNK_OVERLAP_CHARS :]
        if current and len(current) + len(paragraph) + 2 > CHUNK_TARGET_CHARS:
            chunks.append(current)
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
    if current:
        chunks.append(current)
    return chunks


async def vector_enabled(db: AsyncSession) -> bool:
    """Whether this database has the embedding column (pgvector installed)."""
    global _vector_ready
    if _vector_ready:
        return True
    found = (
        await db.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'knowledge_chunks' AND column_name = 'embedding'"
            )
        )
    ).first()
    _vector_ready = found is not None
    return _vector_ready


async def _embed_chunks(db: AsyncSession, chunks: list[KnowledgeChunk]) -> int:
    if not chunks or not await vector_enabled(db):
        return 0
    vectors = embeddings.get_embedder().embed([c.content for c in chunks])
    for chunk, vec in zip(chunks, vectors, strict=True):
        await db.execute(
            update(chunk_vectors)
            .where(chunk_vectors.c.id == chunk.id)
            .values(embedding=cast(vector_literal(vec), Vector()))
        )
    return len(chunks)


async def index_document(db: AsyncSession, document: KnowledgeDocument) -> None:
    await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
    chunks = [
        KnowledgeChunk(
            tenant_id=document.tenant_id,
            document_id=document.id,
            chunk_index=index,
            content=content,
        )
        for index, content in enumerate(chunk_text(document.content))
    ]
    db.add_all(chunks)
    document.chunk_count = len(chunks)
    await db.flush()
    try:
        await _embed_chunks(db, chunks)
        document.status, document.error = "ready", None
    except Exception as exc:  # text search still works without vectors
        logger.warning("embedding failed for document %s", document.id, exc_info=True)
        document.status, document.error = "ready", f"Indexed for keyword search only: {exc}"[:500]
    await db.flush()


async def reindex_embeddings(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Embeds every chunk of the tenant again (after the embedder changes, or for
    documents added before vectors were available)."""
    if not await vector_enabled(db):
        return 0
    chunks = list(
        (await db.execute(select(KnowledgeChunk).where(KnowledgeChunk.tenant_id == tenant_id)))
        .scalars()
        .all()
    )
    done = 0
    for start in range(0, len(chunks), 100):
        done += await _embed_chunks(db, chunks[start : start + 100])
    await db.flush()
    return done


def _fts_query(words: list[str]):
    # Words are \w+ tokens from our own tokenizer, so they are safe inside to_tsquery.
    return func.to_tsquery("english", " | ".join(words))


def _term_match(question_terms: list[str], passage: str) -> float:
    """Share of the question's words the passage covers, counting synonyms as the same word."""
    if not question_terms:
        return 0.0
    passage_terms = set(embeddings.terms(passage))
    hit = sum(1 for t in question_terms if embeddings.synonyms_of(t) & passage_terms)
    return hit / len(question_terms)


def _passes_gate(question_terms: list[str], ratio: float) -> bool:
    if ratio <= 0:
        return False
    return len(question_terms) <= 2 or ratio >= MIN_TERM_MATCH


async def search(
    db: AsyncSession, *, tenant_id: uuid.UUID, query: str, limit: int = 5
) -> list[dict]:
    question_terms = embeddings.terms(query)
    if not question_terms:
        return []
    expanded = embeddings.expand(query)
    # Documents switched off for the AI (guardrails knowledge control) never surface.
    visible = or_(KnowledgeAIControl.enabled.is_(None), KnowledgeAIControl.enabled.is_(True))

    # Match ANY term (OR) and let ts_rank_cd put passages matching more terms first.
    ts_query = _fts_query(expanded)
    rank = func.ts_rank_cd(KnowledgeChunk.search_vector, ts_query)
    text_ids = [
        row[0]
        for row in (
            await db.execute(
                select(KnowledgeChunk.id)
                .outerjoin(
                    KnowledgeAIControl, KnowledgeAIControl.document_id == KnowledgeChunk.document_id
                )
                .where(
                    KnowledgeChunk.tenant_id == tenant_id,
                    KnowledgeChunk.search_vector.op("@@")(ts_query),
                    visible,
                )
                .order_by(rank.desc())
                .limit(CANDIDATES)
            )
        ).all()
    ]

    vector_ids: list[uuid.UUID] = []
    query_vector: list[float] | None = None
    if await vector_enabled(db):
        try:
            query_vector = embeddings.get_embedder().embed([" ".join(expanded)])[0]
        except Exception:  # keyword results are still good; don't fail the question
            logger.warning("could not embed the question; using keyword search only", exc_info=True)
    if query_vector is not None:
        query_vec = cast(vector_literal(query_vector), Vector())
        distance = chunk_vectors.c.embedding.op("<=>")(query_vec)
        vector_ids = [
            row[0]
            for row in (
                await db.execute(
                    select(chunk_vectors.c.id)
                    .outerjoin(
                        KnowledgeAIControl,
                        KnowledgeAIControl.document_id == chunk_vectors.c.document_id,
                    )
                    .where(
                        chunk_vectors.c.tenant_id == tenant_id,
                        chunk_vectors.c.embedding.is_not(None),
                        visible,
                    )
                    .order_by(distance)
                    .limit(CANDIDATES)
                )
            ).all()
        ]

    fused: dict[uuid.UUID, float] = {}
    for ranking in (text_ids, vector_ids):
        for position, chunk_id in enumerate(ranking):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (RRF_K + position + 1)
    if not fused:
        return []

    rows = (
        await db.execute(
            select(KnowledgeChunk, KnowledgeDocument.title, KnowledgeAIControl.visibility)
            .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
            .outerjoin(
                KnowledgeAIControl, KnowledgeAIControl.document_id == KnowledgeChunk.document_id
            )
            .where(KnowledgeChunk.id.in_(list(fused)), KnowledgeChunk.tenant_id == tenant_id)
        )
    ).all()

    best = 2.0 / (RRF_K + 1)  # first place in both rankings
    hits = []
    for chunk, title, visibility in rows:
        ratio = _term_match(question_terms, chunk.content)
        if not _passes_gate(question_terms, ratio):
            continue
        hits.append(
            {
                "document_id": str(chunk.document_id),
                "chunk_id": str(chunk.id),
                "chunk_index": chunk.chunk_index,
                "title": title,
                "content": chunk.content,
                # Fused rank, nudged by how much of the question the passage covers.
                "score": round(min(1.0, fused[chunk.id] / best) * (0.5 + 0.5 * ratio), 4),
                "match": round(ratio, 3),
                "via": "both"
                if chunk.id in text_ids and chunk.id in vector_ids
                else ("text" if chunk.id in text_ids else "vector"),
                "internal_only": visibility == "internal",
            }
        )
    hits.sort(key=lambda h: h["score"], reverse=True)
    return hits[:limit]


def citations(hits: list[dict]) -> list[dict]:
    """The passages an answer drew on, one per document, for showing under the reply."""
    seen: dict[str, dict] = {}
    for h in hits:
        seen.setdefault(
            h["document_id"],
            {"document_id": h["document_id"], "title": h["title"], "chunk_id": h.get("chunk_id")},
        )
    return list(seen.values())
