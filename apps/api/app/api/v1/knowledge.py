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
)
from app.services import knowledge as knowledge_service
from app.services import plans
from app.services.knowledge_extract import MAX_UPLOAD_BYTES, SUFFIXES, ExtractError, extract_text
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import KnowledgeDocument, KnowledgeDocumentSource

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
