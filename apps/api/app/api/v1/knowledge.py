import asyncio
import uuid

from app.core.audit import log_audit_event
from app.core.deps import RequestContext, get_db, require_permission
from app.core.permissions import Permissions
from app.core.repository import TenantScopedRepository
from app.schemas.knowledge import (
    KnowledgeDocumentCreate,
    KnowledgeDocumentDetail,
    KnowledgeDocumentRead,
    KnowledgeSearchHit,
    KnowledgeSearchRequest,
    KnowledgeSourceCreate,
    KnowledgeSourceRead,
    KnowledgeSourceUpdate,
)
from app.services import knowledge as knowledge_service
from app.services import knowledge_sources, plans
from app.services.knowledge_extract import MAX_UPLOAD_BYTES, SUFFIXES, ExtractError, extract_text
from app.services.safe_fetch import FetchError, UnsafeURL, assert_fetchable
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import KnowledgeDocument, KnowledgeDocumentSource, KnowledgeSource

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

ALLOWED_UPLOAD_SUFFIXES = SUFFIXES


class KnowledgeDocumentRepository(TenantScopedRepository[KnowledgeDocument]):
    model = KnowledgeDocument


async def _create_document(
    db: AsyncSession,
    ctx: RequestContext,
    *,
    title: str,
    content: str,
    source: KnowledgeDocumentSource,
) -> KnowledgeDocument:
    await plans.ensure_can_create(db, ctx.tenant_id, "knowledge_documents")
    document = await KnowledgeDocumentRepository(db, ctx.tenant_id).create(
        title=title, content=content, source=source
    )
    await knowledge_service.index_document(db, document)
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="knowledge_document.created",
        resource_type="knowledge_document",
        resource_id=document.id,
        extra={"title": title},
    )
    await db.commit()
    await db.refresh(document)
    return document


@router.post(
    "/documents", response_model=KnowledgeDocumentRead, status_code=status.HTTP_201_CREATED
)
async def create_document(
    payload: KnowledgeDocumentCreate,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeDocument:
    return await _create_document(
        db, ctx, title=payload.title, content=payload.content, source=KnowledgeDocumentSource.TEXT
    )


@router.post(
    "/documents/upload", response_model=KnowledgeDocumentRead, status_code=status.HTTP_201_CREATED
)
async def upload_document(
    file: UploadFile = File(...),
    title: str | None = None,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeDocument:
    filename = file.filename or "document"
    if not filename.lower().endswith(ALLOWED_UPLOAD_SUFFIXES):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unsupported file type; allowed: {', '.join(ALLOWED_UPLOAD_SUFFIXES)}",
        )
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File exceeds 10 MB")
    try:
        # Parsing a large PDF is CPU work; keep it off the event loop.
        content = await asyncio.to_thread(extract_text, filename, raw)
    except ExtractError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return await _create_document(
        db,
        ctx,
        title=(title or filename)[:255],
        content=content,
        source=KnowledgeDocumentSource.UPLOAD,
    )


@router.get("/documents", response_model=list[KnowledgeDocumentRead])
async def list_documents(
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[KnowledgeDocument]:
    return await KnowledgeDocumentRepository(db, ctx.tenant_id).list(limit=limit, offset=offset)


@router.get("/documents/{document_id}", response_model=KnowledgeDocumentDetail)
async def get_document(
    document_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeDocument:
    return await KnowledgeDocumentRepository(db, ctx.tenant_id).get(document_id)


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    document = await KnowledgeDocumentRepository(db, ctx.tenant_id).get(document_id)
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="knowledge_document.deleted",
        resource_type="knowledge_document",
        resource_id=document.id,
        extra={"title": document.title},
    )
    await db.delete(document)
    await db.commit()


MAX_ACTIVE_SOURCES = 10


class KnowledgeSourceRepository(TenantScopedRepository[KnowledgeSource]):
    model = KnowledgeSource


@router.post(
    "/sources", response_model=KnowledgeSourceRead, status_code=status.HTTP_201_CREATED
)
async def create_source(
    payload: KnowledgeSourceCreate,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeSource:
    """Starts reading a website into the knowledge base. Pages are fetched in the
    background; poll the source for progress."""
    url = payload.url.strip()
    if "://" not in url:
        url = f"https://{url}"
    try:
        await assert_fetchable(url)
    except UnsafeURL as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except FetchError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await plans.ensure_can_create(db, ctx.tenant_id, "knowledge_documents")
    count = (
        await db.execute(
            select(func.count(KnowledgeSource.id)).where(KnowledgeSource.tenant_id == ctx.tenant_id)
        )
    ).scalar_one()
    if count >= MAX_ACTIVE_SOURCES:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"You can have up to {MAX_ACTIVE_SOURCES} website sources"
        )
    source = await KnowledgeSourceRepository(db, ctx.tenant_id).create(
        url=url,
        page_limit=payload.page_limit,
        resync_weekly=payload.resync_weekly,
        status="queued",
    )
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="knowledge_source.created",
        resource_type="knowledge_source",
        resource_id=source.id,
        extra={"url": url},
    )
    await db.commit()
    await db.refresh(source)
    knowledge_sources.enqueue_crawl(source.id)
    return source


@router.get("/sources", response_model=list[KnowledgeSourceRead])
async def list_sources(
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[KnowledgeSource]:
    rows = await db.execute(
        select(KnowledgeSource)
        .where(KnowledgeSource.tenant_id == ctx.tenant_id)
        .order_by(KnowledgeSource.created_at.desc())
    )
    return list(rows.scalars().all())


@router.get("/sources/{source_id}", response_model=KnowledgeSourceRead)
async def get_source(
    source_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeSource:
    return await KnowledgeSourceRepository(db, ctx.tenant_id).get(source_id)


@router.patch("/sources/{source_id}", response_model=KnowledgeSourceRead)
async def update_source(
    source_id: uuid.UUID,
    payload: KnowledgeSourceUpdate,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeSource:
    source = await KnowledgeSourceRepository(db, ctx.tenant_id).get(source_id)
    if payload.resync_weekly is not None:
        source.resync_weekly = payload.resync_weekly
    await db.commit()
    await db.refresh(source)
    return source


@router.post("/sources/{source_id}/resync", response_model=KnowledgeSourceRead)
async def resync_source(
    source_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeSource:
    source = await KnowledgeSourceRepository(db, ctx.tenant_id).get(source_id)
    if source.status in ("queued", "crawling"):
        raise HTTPException(status.HTTP_409_CONFLICT, "This website is already being read")
    source.status, source.error = "queued", None
    await db.commit()
    await db.refresh(source)
    knowledge_sources.enqueue_crawl(source.id)
    return source


@router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(
    source_id: uuid.UUID,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Removes the source and every page that came from it."""
    source = await KnowledgeSourceRepository(db, ctx.tenant_id).get(source_id)
    await log_audit_event(
        db,
        tenant_id=ctx.tenant_id,
        actor_user_id=ctx.user_id,
        action="knowledge_source.deleted",
        resource_type="knowledge_source",
        resource_id=source.id,
        extra={"url": source.url},
    )
    await db.delete(source)
    await db.commit()


@router.post("/reindex")
async def reindex(
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Embeds every passage again, for documents added before vector search was available."""
    count = await knowledge_service.reindex_embeddings(db, ctx.tenant_id)
    await db.commit()
    return {"passages_embedded": count, "vector_search": await knowledge_service.vector_enabled(db)}


@router.post("/search", response_model=list[KnowledgeSearchHit])
async def search_knowledge(
    payload: KnowledgeSearchRequest,
    ctx: RequestContext = Depends(require_permission(Permissions.AGENTS_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    return await knowledge_service.search(
        db, tenant_id=ctx.tenant_id, query=payload.query, limit=payload.limit
    )
