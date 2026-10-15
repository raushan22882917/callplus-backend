import enum
import uuid

from sqlalchemy import Computed, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class KnowledgeDocumentSource(str, enum.Enum):
    TEXT = "text"
    UPLOAD = "upload"


class KnowledgeDocument(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A tenant-owned document an AI agent can search (FAQs, policies, product
    docs). The full text is kept so chunks can be rebuilt if the chunking
    strategy changes."""

    __tablename__ = "knowledge_documents"

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[KnowledgeDocumentSource] = mapped_column(
        Enum(KnowledgeDocumentSource, name="knowledge_document_source"),
        default=KnowledgeDocumentSource.TEXT,
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class KnowledgeChunk(Base, UUIDPrimaryKeyMixin, TenantScopedMixin):
    __tablename__ = "knowledge_chunks"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', content)", persisted=True), nullable=False
    )

    __table_args__ = (
        Index("ix_knowledge_chunks_search_vector", "search_vector", postgresql_using="gin"),
    )
