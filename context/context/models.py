"""Data model for stored context items."""

from __future__ import annotations

import hashlib
from enum import Enum

from pydantic import BaseModel, Field


class StrEnum(str, Enum):
    """str+Enum base, compatible with Python 3.10 (enum.StrEnum is 3.11+)."""

    def __str__(self) -> str:
        return self.value


class Kind(StrEnum):
    """What a stored fact is — the salience judge (#17) tags each write."""

    DECISION = "decision"      # a choice made and why
    CONSTRAINT = "constraint"  # a rule/limit the project must respect
    OUTCOME = "outcome"        # a result: what happened, what worked/failed
    HANDOFF = "handoff"        # state to carry into a later session


def content_hash(text: str) -> str:
    """Stable hash of normalized text, for dedup within a partition."""
    normalized = " ".join(text.split()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class ContextItem(BaseModel):
    """One durable fact in the store. `embedding` is filled by the embed step."""

    partition: str
    kind: Kind
    text: str
    provenance: dict[str, str] = Field(default_factory=dict)  # session id, source, ts, repo path
    embedding: list[float] | None = None

    @property
    def hash(self) -> str:
        return content_hash(self.text)


class SearchHit(BaseModel):
    """A retrieved item with its similarity score (1.0 = identical direction)."""

    kind: Kind
    text: str
    provenance: dict[str, str] = Field(default_factory=dict)
    score: float
