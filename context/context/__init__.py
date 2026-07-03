"""Divide-and-conquer long-term context store.

Shared semantic memory for coding sessions, partitioned by repo/parent-folder.
Durable facts (decisions, constraints, outcomes, handoffs) are embedded via the
fleet's `embed:qwen3` model and stored in Postgres + pgvector; vague prompts are
enriched by retrieving the most relevant prior context for the current repo.

Layering (pure logic separated from I/O so it unit-tests without a live DB/network):
- `partition`  — derive a stable repo partition key (git origin -> root -> cwd)
- `models`     — ContextItem + Kind
- `embed`      — OpenAI-compatible /embeddings client (LiteLLM `embed:qwen3`)
- `store`      — pgvector schema, dedup insert, top-k similarity search
- `service`    — ties embed + store: remember() / recall()
"""
