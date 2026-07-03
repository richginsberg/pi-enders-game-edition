"""Embedding client for the fleet's `embed:qwen3` model via LiteLLM.

OpenAI-compatible /embeddings. Endpoint/model/key come from env so no local
settings are committed (open-source hygiene). Whichever backend DNC_EMBED_BASE
points at — an S3 GPU node, the 9700T CPU llama-server, or DeepInfra — is
transparent here; see tools/bench_embed.py for the provider decision.

`build_request` is pure (unit-testable); `embed_texts` does the HTTP.
"""

from __future__ import annotations

import json
import os
import urllib.request

EMBED_BASE = os.environ.get("DNC_EMBED_BASE", "http://localhost:4000/v1")
EMBED_MODEL = os.environ.get("DNC_EMBED_MODEL", "embed:qwen3")
EMBED_KEY = os.environ.get("DNC_EMBED_KEY")  # "none"/unset for self-host; real key only for a vendor
EMBED_DIM = int(os.environ.get("DNC_EMBED_DIM", "1024"))  # Qwen3-Embedding-0.6B = 1024


def build_request(texts: list[str], model: str = EMBED_MODEL) -> dict:
    """The OpenAI /embeddings request body for a batch of inputs."""
    return {"model": model, "input": texts}


def _post(base: str, body: dict, key: str | None) -> dict:
    req = urllib.request.Request(f"{base}/embeddings", data=json.dumps(body).encode(), method="POST")
    req.add_header("content-type", "application/json")
    if key and key != "none":
        req.add_header("authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def embed_texts(
    texts: list[str], *, base: str = EMBED_BASE, model: str = EMBED_MODEL, key: str | None = EMBED_KEY
) -> list[list[float]]:
    """Embed a batch, returning one vector per input in the same order."""
    if not texts:
        return []
    payload = _post(base, build_request(texts, model), key)
    # OpenAI response: data is a list of {index, embedding}; order by index to be safe.
    rows = sorted(payload["data"], key=lambda d: d["index"])
    return [r["embedding"] for r in rows]


def embed_one(text: str, **kw) -> list[float]:
    return embed_texts([text], **kw)[0]
