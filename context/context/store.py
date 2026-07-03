"""Postgres + pgvector store for context items, scoped by partition.

Pure SQL/format helpers are module-level (unit-testable); `Store` holds the psycopg
connection and is imported lazily so the package works without the driver installed.

Requires pgvector >= 0.5 (for the HNSW index). Connection string from env
(DNC_PG_DSN), never committed.
"""

from __future__ import annotations

import json
import os

from .models import ContextItem, Kind, SearchHit

PG_DSN = os.environ.get("DNC_PG_DSN", "postgresql:///dnc_context")


def vector_literal(vec: list[float]) -> str:
    """pgvector text form: '[0.1,0.2,...]'. Cast with ::vector in SQL."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def schema_sql(dim: int) -> str:
    """DDL for the store. Idempotent; `partition` is a non-reserved keyword in PG."""
    return f"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS context_items (
    id           BIGSERIAL PRIMARY KEY,
    partition    TEXT NOT NULL,
    kind         TEXT NOT NULL,
    text         TEXT NOT NULL,
    provenance   JSONB NOT NULL DEFAULT '{{}}',
    embedding    VECTOR({dim}) NOT NULL,
    content_hash TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (partition, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_context_partition ON context_items (partition);
CREATE INDEX IF NOT EXISTS idx_context_embedding
    ON context_items USING hnsw (embedding vector_cosine_ops);
"""


INSERT_SQL = """
INSERT INTO context_items (partition, kind, text, provenance, embedding, content_hash)
VALUES (%s, %s, %s, %s::jsonb, %s::vector, %s)
ON CONFLICT (partition, content_hash) DO NOTHING
RETURNING id;
"""

SEARCH_SQL = """
SELECT kind, text, provenance, 1 - (embedding <=> %s::vector) AS score
FROM context_items
WHERE partition = %s
ORDER BY embedding <=> %s::vector
LIMIT %s;
"""


class Store:
    def __init__(self, dsn: str = PG_DSN, dim: int = 0) -> None:
        import psycopg  # lazy: package usable without the driver for pure helpers/tests

        from .embed import EMBED_DIM

        self.dim = dim or EMBED_DIM
        self.conn = psycopg.connect(dsn, autocommit=True)

    def ensure_schema(self) -> None:
        self.conn.execute(schema_sql(self.dim))

    def add(self, items: list[ContextItem]) -> int:
        """Insert embedded items; dedup by (partition, content_hash). Returns rows added."""
        added = 0
        with self.conn.cursor() as cur:
            for it in items:
                if it.embedding is None:
                    raise ValueError(f"item not embedded: {it.text[:60]!r}")
                cur.execute(
                    INSERT_SQL,
                    (
                        it.partition,
                        str(it.kind),
                        it.text,
                        json.dumps(it.provenance),
                        vector_literal(it.embedding),
                        it.hash,
                    ),
                )
                if cur.fetchone() is not None:  # RETURNING id present only on actual insert
                    added += 1
        return added

    def search(self, partition: str, query_embedding: list[float], k: int = 5) -> list[SearchHit]:
        lit = vector_literal(query_embedding)
        with self.conn.cursor() as cur:
            cur.execute(SEARCH_SQL, (lit, partition, lit, k))
            rows = cur.fetchall()
        return [
            SearchHit(kind=Kind(kind), text=text, provenance=prov or {}, score=float(score))
            for (kind, text, prov, score) in rows
        ]

    def close(self) -> None:
        self.conn.close()
