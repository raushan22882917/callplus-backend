"""Optional pgvector support for knowledge-base embeddings.

The `embedding` column is deliberately not on the ORM model: a database without
the `vector` extension (some managed Postgres images) must keep working, with
search falling back to full text. This module owns the column, the type, and the
one place that tries to create them."""

from sqlalchemy import column, table, text
from sqlalchemy.engine import Connection
from sqlalchemy.types import UserDefinedType

EMBEDDING_DIM = 384


class Vector(UserDefinedType):
    cache_ok = True

    def __init__(self, dim: int = EMBEDDING_DIM):
        self.dim = dim

    def get_col_spec(self, **_kw) -> str:
        return f"vector({self.dim})"


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"


# A light view of the table with just what vector reads and writes need.
chunk_vectors = table(
    "knowledge_chunks",
    column("id"),
    column("tenant_id"),
    column("document_id"),
    column("embedding", Vector()),
)


def ensure_embedding_column(conn: Connection) -> bool:
    """Adds the extension, column and index if the server offers pgvector.
    Returns False (and changes nothing) when it does not."""
    available = conn.execute(
        text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
    ).first()
    if not available:
        return False
    # Installing an extension needs privileges the app's role may not have. Try it in a
    # savepoint so a refusal leaves the migration (and the deploy) intact.
    try:
        with conn.begin_nested():
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    except Exception:
        return False
    conn.execute(
        text(f"ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS embedding vector({EMBEDDING_DIM})")
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_embedding "
            "ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)"
        )
    )
    return True
