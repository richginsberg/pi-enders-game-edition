"""HTTP sidecar for the context store, consumed by the Pi extension.

Kept separate from fleetd: this owns the pgvector connection and the embedding
calls. Endpoints are sync (FastAPI runs them in a threadpool); each opens its own
short-lived Store so we never share a psycopg connection across threads. Volume is
low (recall on vague prompts, writes at milestone/session end), so per-request
connect cost is fine.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel, Field

from . import embed as _embed
from . import judge as _judge
from .models import ContextItem, Kind, SearchHit
from .partition import detect_partition, resolve_partition
from .store import Store

app = FastAPI(title="dnc-context", version="0.1.0")


class RecallRequest(BaseModel):
    query: str
    k: int = 5
    cwd: str | None = None       # working dir of the requesting session -> partition
    partition: str | None = None  # explicit override


class FactIn(BaseModel):
    kind: Kind
    text: str
    provenance: dict[str, str] = Field(default_factory=dict)


class RememberRequest(BaseModel):
    items: list[FactIn]
    cwd: str | None = None
    partition: str | None = None


class DistillRequest(BaseModel):
    transcript: str
    provenance: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    partition: str | None = None


def _store_items(items: list[ContextItem]) -> int:
    """Embed any un-embedded items and persist them; returns rows added."""
    if not items:
        return 0
    to_embed = [it for it in items if it.embedding is None]
    if to_embed:
        for it, v in zip(to_embed, _embed.embed_texts([it.text for it in to_embed])):
            it.embedding = v
    store = Store()
    try:
        store.ensure_schema()
        return store.add(items)
    finally:
        store.close()


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/partition")
def partition(cwd: str | None = None) -> dict:
    return {"partition": detect_partition(Path(cwd) if cwd else None)}


@app.post("/recall")
def recall(req: RecallRequest) -> list[SearchHit]:
    part = resolve_partition(req.partition, req.cwd)
    store = Store()
    try:
        store.ensure_schema()
        return store.search(part, _embed.embed_one(req.query), req.k)
    finally:
        store.close()


@app.post("/remember")
def remember(req: RememberRequest) -> dict:
    part = resolve_partition(req.partition, req.cwd)
    items = [ContextItem(partition=part, kind=f.kind, text=f.text, provenance=f.provenance) for f in req.items]
    return {"partition": part, "added": _store_items(items)}


@app.post("/distill")
def distill(req: DistillRequest) -> dict:
    """Run the salience judge over a transcript and persist the durable facts.

    Called at milestone (harness) and session end (extension). The judge decides
    what's worth keeping; an empty transcript or a low-value session stores nothing.
    """
    part = resolve_partition(req.partition, req.cwd)
    facts = _judge.distill(req.transcript)
    items = [
        ContextItem(partition=part, kind=kind, text=text, provenance=req.provenance) for kind, text in facts
    ]
    return {"partition": part, "facts": len(facts), "added": _store_items(items)}


def serve() -> None:
    import uvicorn

    uvicorn.run(app, host=os.environ.get("DNC_CONTEXT_HOST", "127.0.0.1"),
                port=int(os.environ.get("DNC_CONTEXT_PORT", "7432")))
