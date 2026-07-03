from context.embed import build_request
from context.models import ContextItem, Kind, content_hash
from context.store import SEARCH_SQL, schema_sql, vector_literal


def test_vector_literal_format():
    assert vector_literal([0.5, -1.0, 2]) == "[0.5,-1.0,2.0]"


def test_schema_embeds_dimension():
    sql = schema_sql(1024)
    assert "VECTOR(1024)" in sql
    assert "hnsw (embedding vector_cosine_ops)" in sql
    assert "UNIQUE (partition, content_hash)" in sql


def test_content_hash_is_normalized():
    # whitespace + case collapse to the same hash -> dedup catches near-identical text
    assert content_hash("Chose  pgvector\nover sqlite") == content_hash("chose pgvector over sqlite")
    assert content_hash("a") != content_hash("b")


def test_item_hash_uses_content_hash():
    it = ContextItem(partition="p", kind=Kind.DECISION, text="pick pgvector")
    assert it.hash == content_hash("pick pgvector")


def test_search_sql_scopes_by_partition_and_orders_by_distance():
    assert "WHERE partition = %s" in SEARCH_SQL
    assert "ORDER BY embedding <=> %s::vector" in SEARCH_SQL
    assert "1 - (embedding <=> %s::vector) AS score" in SEARCH_SQL


def test_build_embed_request():
    assert build_request(["a", "b"], model="embed:qwen3") == {"model": "embed:qwen3", "input": ["a", "b"]}


def test_kind_str():
    assert str(Kind.CONSTRAINT) == "constraint"
