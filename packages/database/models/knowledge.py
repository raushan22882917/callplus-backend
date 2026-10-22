import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, Computed, DateTime, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.sql import expression
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.base import Base
from packages.database.mixins import TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class KnowledgeDocumentSource(str, enum.Enum):
    TEXT = "text"
    UPLOAD = "upload"


class KnowledgeSource(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A website the knowledge base is built from. Each crawled page becomes a
    document (`knowledge_documents.source_id`); the source carries the progress."""

    __tablename__ = "knowledge_sources"

    kind: Mapped[str] = mapped_column(String(10), default="website", nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    # queued -> crawling -> ready | failed
    status: Mapped[str] = mapped_column(String(10), default="queued", nullable=False)
    page_limit: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    pages_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pages_done: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    resync_weekly: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=expression.true(), nullable=False
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeDocument(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A tenant-owned document an AI agent can search (FAQs, policies, product
    docs). The full text is kept so chunks can be rebuilt if the chunking
    strategy changes."""

    __tablename__ = "knowledge_documents"

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[KnowledgeDocumentSource] = mapped_column(
        # The migration created this type with the lowercase VALUES ("text", "upload"), while
        # SQLAlchemy writes member NAMES by default ("TEXT"), which Postgres rejected: adding a
        # document failed on every migrated database. Write the values so the two agree.
        Enum(
            KnowledgeDocumentSource,
            name="knowledge_document_source",
            values_callable=lambda members: [m.value for m in members],
        ),
        default=KnowledgeDocumentSource.TEXT,
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # processing -> ready | failed. Indexing is quick for text and files, but a long
    # source (a crawl, a big PDF) shows its progress here and a failure shows its reason.
    status: Mapped[str] = mapped_column(String(10), default="ready", server_default="ready", nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    # Set for pages that came from a crawled website; deleting the source deletes them.
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_sources.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str | None] = mapped_column(String(2048))


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
