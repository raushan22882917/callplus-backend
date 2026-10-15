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
    created_at: datetime


class KnowledgeDocumentDetail(KnowledgeDocumentRead):
    content: str


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=20)


class KnowledgeSearchHit(BaseModel):
    document_id: uuid.UUID
    title: str
    content: str
    score: float
