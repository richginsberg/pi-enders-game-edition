"""FastAPI surface for burst orchestration.

Mounted into fleetd (`app.include_router(burst.api.router)`) so the whole control plane stays
one daemon, but importable standalone — `uvicorn burst.api:app`.

Shape mirrors the existing fleet-power API on purpose: a dry-run `plan` you can inspect
before spending anything, an SSE `stream` for progress, and a `status` for what's live.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .drivers import get_driver
from .lifecycle import BurstManager
from .policy import Demand, Policy, should_burst
from .profiles import DEFAULT_MODEL, GPUS, MODELS, plan_capacity, rank_gpus
from .template import render

router = APIRouter(prefix="/burst", tags=["burst"])
_manager: BurstManager | None = None


def policy_from_env() -> Policy:
    e = os.environ.get
    return Policy(
        enabled=e("BURST_ENABLED", "false").lower() in ("1", "true", "yes"),
        min_output_tokens=int(e("BURST_MIN_OUTPUT_TOKENS", 5_000_000)),
        min_duration_hours=float(e("BURST_MIN_HOURS", 1.0)),
        idle_ttl_s=int(e("BURST_IDLE_TTL_S", 900)),
        max_lifetime_s=int(e("BURST_MAX_LIFETIME_S", 6 * 3600)),
        max_replicas=int(e("BURST_MAX_REPLICAS", 4)),
        daily_usd_cap=float(e("BURST_DAILY_USD_CAP", 50.0)),
        volume_max_idle_days=int(e("BURST_VOLUME_MAX_IDLE_DAYS", 14)),
        per_node_tok_s=float(e("BURST_PER_NODE_TOK_S", 0.0)),
    )


def manager() -> BurstManager:
    global _manager
    if _manager is None:
        _manager = BurstManager(
            driver=get_driver(os.environ.get("BURST_PROVIDER", "runpod")),
            policy=policy_from_env(),
        )
    return _manager


class GateRequest(BaseModel):
    projected_output_tokens: int
    projected_hours: float
    expected_saturation: float = 1.0
    expected_tok_s: float = 0.0
    fallback_usd_per_m_out: float = 10.0
    gpu: str = "rtxpro6000"


class UpRequest(BaseModel):
    model: str = DEFAULT_MODEL
    quant: str = "autoround"
    gpu: str | None = None
    context: int = 262144
    replicas: int = 1


@router.get("/models")
def list_models() -> list[dict]:
    """Servable models with the geometry that drives capacity planning."""
    return [{
        "name": m.name, "total_params_b": m.total_params_b,
        "active_params_b": m.active_params_b, "is_moe": m.is_moe,
        "swe_bench_verified": m.swe_bench_verified,
        "max_context": m.max_context,
        "kv_bytes_per_token_fp8": m.kv_bytes_per_token(1),
        "quants": sorted(m.quants),
    } for m in MODELS.values()]


@router.get("/capacity")
def capacity(model: str = DEFAULT_MODEL, quant: str = "autoround",
             context: int = 262144, min_seqs: int = 8) -> dict:
    """Which cards can serve this at `context` with >= min_seqs concurrency, cheapest first.
    This is how you pick a card without guessing at --max-num-seqs."""
    if model not in MODELS:
        raise HTTPException(404, f"unknown model {model}")
    plans = rank_gpus(model, quant, context=context, min_seqs=min_seqs)
    return {"model": model, "quant": quant, "context": context, "min_seqs": min_seqs,
            "options": [{
                "gpu": p.gpu, "usd_per_hr": GPUS[p.gpu].usd_per_hr, "vram_gb": GPUS[p.gpu].vram_gb,
                "max_num_seqs": p.max_num_seqs, "kv_pool_gib": p.kv_pool_gib,
                "token_pool": p.token_pool,
            } for p in plans]}


@router.get("/template")
def template(model: str = DEFAULT_MODEL, quant: str = "autoround",
             gpu: str = "rtxpro6000", context: int = 262144,
             profile: str = "saturation") -> dict:
    """The injectable launch template, with --max-num-seqs computed for this card."""
    try:
        return render(model, quant, gpu, context=context, profile=profile).as_dict()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/gate")
def gate(req: GateRequest) -> dict:
    """The determination: is this workload big and saturated enough to justify renting?"""
    mgr = manager()
    econ = mgr.economics(req.gpu, req.expected_tok_s, req.fallback_usd_per_m_out)
    d = should_burst(
        Demand(req.projected_output_tokens, req.projected_hours, req.expected_saturation),
        econ, mgr.policy, spent_today_usd=mgr.spent_today_usd)
    return {
        "verdict": d.verdict, "reason": d.reason, "replicas": d.replicas,
        "projected_cost_usd": round(d.projected_cost_usd, 2),
        "fallback_cost_usd": round(d.fallback_cost_usd, 2),
        "savings_usd": round(d.savings_usd, 2),
        "effective_usd_per_m": round(econ.effective_usd_per_m(), 4),
        "breakeven_tok_s": round(econ.breakeven_tok_s(), 1),
    }


@router.get("/status")
def status() -> dict:
    return manager().status()


@router.post("/up/stream")
async def up_stream(req: UpRequest) -> StreamingResponse:
    """Rent + serve, streaming progress as SSE. The node is registered into the gateway only
    once /v1/models answers; until then the tier falls up to the higher tier."""
    mgr = manager()
    if not mgr.policy.enabled:
        raise HTTPException(403, "burst is not enabled (set BURST_ENABLED=true)")

    async def gen() -> AsyncIterator[str]:
        async for ev in mgr.scale_up(model=req.model, quant=req.quant, gpu_key=req.gpu,
                                     context=req.context, replicas=req.replicas):
            yield f"data: {json.dumps(ev, default=str)}\n\n"
        yield f"data: {json.dumps({'type': 'done', **mgr.status()}, default=str)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/down")
async def down(node_id: str | None = None) -> dict:
    """Release one node, or all of them."""
    mgr = manager()
    targets = [mgr.nodes[node_id]] if node_id else list(mgr.nodes.values())
    if node_id and node_id not in mgr.nodes:
        raise HTTPException(404, f"no such burst node {node_id}")
    return {"released": [await mgr.release(n, "manual") for n in targets]}


@router.post("/reap")
async def reap() -> dict:
    """One reaper pass: idle-TTL and hard-lifetime kills. Run this on a timer."""
    mgr = manager()
    return {"released": await mgr.reap(),
            "orphans": [n.node_id for n in mgr.reconcile()]}


app = FastAPI(title="dnc-burst", version="0.1.0")
app.include_router(router)
