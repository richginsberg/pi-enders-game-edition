"""OpenAI-compatible FastAPI surface. Routes each model to its owning module."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from .registry import build_modules, find_module

CONFIG_PATH = Path(os.environ.get("DNC_CLIPROXY_CONFIG", "cliproxy.yaml")).expanduser()

app = FastAPI(title="dnc-cliproxy", version="0.1.0")
_config = yaml.safe_load(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {"modules": []}
MODULES = build_modules(_config)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "modules": [m.name for m in MODULES]}


@app.get("/v1/models")
def list_models() -> dict:
    data = [entry for m in MODULES for entry in m.models()]
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    model = body.get("model", "")
    module = find_module(MODULES, model)
    if module is None:
        raise HTTPException(404, f"no module serves model {model!r}")
    try:
        if body.get("stream"):
            return StreamingResponse(module.chat_stream(body), media_type="text/event-stream")
        return await module.chat(body)
    except HTTPException:
        raise
    except Exception as exc:  # surface upstream failures as 502, don't 500 opaquely
        raise HTTPException(502, f"{module.name} upstream error: {exc}") from exc
