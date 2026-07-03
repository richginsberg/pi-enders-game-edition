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
    items = [
        ContextItem(partition=part, kind=f.kind, text=f.text, provenance=f.provenance) for f in req.items
    ]
    vecs = _embed.embed_texts([it.text for it in items])
    for it, v in zip(items, vecs):
        it.embedding = v
    store = Store()
    try:
        store.ensure_schema()
        added = store.add(items)
    finally:
        store.close()
    return {"partition": part, "added": added}


def serve() -> None:
    import uvicorn

    uvicorn.run(app, host=os.environ.get("DNC_CONTEXT_HOST", "127.0.0.1"),
                port=int(os.environ.get("DNC_CONTEXT_PORT", "7432")))
