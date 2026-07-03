"""High-level API tying embedding + store + partition together.

This is what the vague-prompt injection (#16) and salience-judge write path (#17)
call: `recall(query)` to enrich a prompt, `remember(items)` to persist durable facts.
"""

from __future__ import annotations

from . import embed as _embed
from .models import ContextItem, Kind, SearchHit
from .partition import detect_partition
from .store import Store


class ContextService:
    def __init__(self, store: Store | None = None, partition: str | None = None) -> None:
        self.store = store or Store()
        self.store.ensure_schema()
        self.partition = partition or detect_partition()

    def remember(self, items: list[ContextItem]) -> int:
        """Persist durable facts. Embeds any item missing a vector (one batch call).

        Items default to the service's partition. Dedup is handled by the store,
        so re-remembering the same fact is a no-op. Returns rows actually added.
        """
        if not items:
            return 0
        for it in items:
            if not it.partition:
                it.partition = self.partition
        to_embed = [it for it in items if it.embedding is None]
        if to_embed:
            vecs = _embed.embed_texts([it.text for it in to_embed])
            for it, v in zip(to_embed, vecs):
                it.embedding = v
        return self.store.add(items)

    def remember_facts(self, facts: list[tuple[Kind, str]], provenance: dict[str, str] | None = None) -> int:
        """Convenience: persist (kind, text) pairs with shared provenance."""
        prov = provenance or {}
        return self.remember(
            [ContextItem(partition=self.partition, kind=kind, text=text, provenance=prov) for kind, text in facts]
        )

    def recall(self, query: str, k: int = 5) -> list[SearchHit]:
        """Retrieve the most relevant prior context in this partition for a query."""
        return self.store.search(self.partition, _embed.embed_one(query), k)

    def close(self) -> None:
        self.store.close()
