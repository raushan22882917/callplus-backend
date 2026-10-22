import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from packages.database.models import KnowledgeDocumentSource


class KnowledgeDocumentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=500_000)


class KnowledgeDocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    source: KnowledgeDocumentSource
    chunk_count: int
    status: str
    error: str | None
    url: str | None = None
    source_id: uuid.UUID | None = None
    created_at: datetime


class KnowledgeDocumentDetail(KnowledgeDocumentRead):
    content: str


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=20)


class KnowledgeSearchHit(BaseModel):
    document_id: uuid.UUID
    chunk_id: uuid.UUID | None = None
    chunk_index: int | None = None
    title: str
    content: str
    score: float
    match: float | None = None
    via: str | None = None  # text | vector | both


class KnowledgeSourceCreate(BaseModel):
    url: str = Field(min_length=4, max_length=2048)
    page_limit: int = Field(default=50, ge=1, le=200)
    resync_weekly: bool = True


class KnowledgeSourceUpdate(BaseModel):
    resync_weekly: bool | None = None


class KnowledgeSourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    url: str
    status: str
    page_limit: int
    pages_found: int
    pages_done: int
    error: str | None
    resync_weekly: bool
    last_synced_at: datetime | None
    created_at: datetime
