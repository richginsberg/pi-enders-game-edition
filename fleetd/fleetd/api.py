"""HTTP API consumed by the Pi extension (`/fleet`, `/deploy`, `/tasks`) and the harness."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .db import Db
from .models import Deployment, Host, Management, TaskRecord

DB_PATH = Path(os.environ.get("FLEETD_DB", "~/.local/share/fleetd/fleet.sqlite3")).expanduser()

app = FastAPI(title="fleetd", version="0.1.0")
db = Db(DB_PATH)


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


async def stream_play(runner: Callable[[Callable[[dict], None]], Awaitable[dict]]) -> StreamingResponse:
    """Drive a play in a task, relaying each step as a Server-Sent Event.

    `runner(sink)` runs the play (passing `sink` as the play's on_step) and returns
    a dict of final fields merged into the terminating `done` event. Steps arrive as
    {"type":"step",...}; the stream ends with {"type":"done",...} or {"type":"error"}.
    """
    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    async def drive() -> None:
        try:
            final = await runner(queue.put_nowait)  # sink pushes step dicts as they happen
            queue.put_nowait({"type": "done", **final})
        except Exception as exc:  # surface play failures to the client, don't hang the stream
            queue.put_nowait({"type": "error", "detail": str(exc)})
        finally:
            queue.put_nowait(None)  # sentinel: generator stops

    task = asyncio.create_task(drive())

    async def gen() -> AsyncIterator[str]:
        try:
            while (item := await queue.get()) is not None:
                yield _sse({"type": "step", **item} if "type" not in item else item)
        finally:
            await task

    return StreamingResponse(gen(), media_type="text/event-stream")


def _resolve(dep_id: str) -> tuple[Deployment, Host]:
    dep = next((d for d in db.list_deployments() if d.id == dep_id), None)
    if dep is None:
        raise HTTPException(404, f"unknown deployment {dep_id}")
    host = next((h for h in db.list_hosts() if h.id == dep.host_id), None)
    if host is None:
        raise HTTPException(409, f"deployment references unknown host {dep.host_id}")
    return dep, host


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "ts": time.time()}


# -- inventory -----------------------------------------------------------------
@app.get("/hosts")
def list_hosts() -> list[Host]:
    return db.list_hosts()


@app.put("/hosts/{host_id}")
def upsert_host(host_id: str, host: Host) -> Host:
    if host.id != host_id:
        raise HTTPException(400, "id mismatch")
    db.upsert_host(host)
    return host


@app.get("/deployments")
def list_deployments(host_id: str | None = None) -> list[Deployment]:
    return db.list_deployments(host_id)


@app.put("/deployments/{dep_id}")
def upsert_deployment(dep_id: str, dep: Deployment) -> Deployment:
    if dep.id != dep_id:
        raise HTTPException(400, "id mismatch")
    db.upsert_deployment(dep)
    return dep


@app.post("/deployments/{dep_id}/apply")
async def apply_deployment(dep_id: str) -> dict:
    """Run the deploy play (pull image, fetch model, replace container, health poll)."""
    from . import plays

    dep, host = _resolve(dep_id)
    dep.status = "deploying"
    db.upsert_deployment(dep)
    report = await plays.deploy(host, dep)
    dep.status = "healthy" if report.ok else "unhealthy"
    db.upsert_deployment(dep)
    # TODO(task #8/#10): on success register the model with LiteLLM (litellm_sync).
    return {"ok": report.ok, "steps": report.steps}


@app.post("/deployments/{dep_id}/apply/stream")
async def apply_deployment_stream(dep_id: str) -> StreamingResponse:
    """Same as /apply, but streams each play step as it happens (Server-Sent Events)."""
    from . import plays

    dep, host = _resolve(dep_id)  # raise before the stream opens, so 404s stay clean

    async def runner(sink: Callable[[dict], None]) -> dict:
        dep.status = "deploying"
        db.upsert_deployment(dep)
        report = await plays.deploy(host, dep, on_step=sink)
        dep.status = "healthy" if report.ok else "unhealthy"
        db.upsert_deployment(dep)
        return {"ok": report.ok}

    return await stream_play(runner)


@app.post("/hosts/{host_id}/preflight")
async def preflight_host(host_id: str) -> dict:
    """SSH in and verify Docker + GPU driver on a (possibly new) host."""
    from . import plays

    host = next((h for h in db.list_hosts() if h.id == host_id), None)
    if host is None:
        raise HTTPException(404, f"unknown host {host_id}")
    return await plays.preflight(host)


# -- discovery / adoption -----------------------------------------------------------
@app.post("/hosts/{host_id}/discover")
async def discover(host_id: str) -> list[Deployment]:
    """Find pre-existing inference servers on the host and catalog them as adopted.

    Adopted deployments are monitor-only: plays never touch their lifecycle.
    Use the migration flow (M3, task #8) to convert one to a managed deployment.
    """
    from . import discover as disc

    host = next((h for h in db.list_hosts() if h.id == host_id), None)
    if host is None:
        raise HTTPException(404, f"unknown host {host_id}")
    found = await disc.discover_host(host)
    existing_ids = {d.id for d in db.list_deployments(host_id)}
    for dep in found:
        if dep.id not in existing_ids:  # don't clobber facts on re-discovery
            db.upsert_deployment(dep)
    return found


@app.get("/deployments/{dep_id}/migration-plan")
def migration_plan(dep_id: str, new_port: int, target_version: str = "latest") -> dict:
    """Preview the standard deployment an adopted server would migrate to (task #10 diff)."""
    from . import plays

    dep, host = _resolve(dep_id)
    proposed = plays.plan_migration(dep, new_port=new_port, target_version=target_version)
    return {"proposed": proposed.model_dump(), "diff": plays.migration_diff(host, dep, proposed)}


async def _run_migration(
    dep: Deployment, host: Host, new_port: int, target_version: str,
    on_step: Callable[[dict], None] | None = None,
) -> tuple[bool, str | None]:
    """Persist the migration lifecycle around the play. Returns (ok, managed_id)."""
    from . import plays

    dep.management = Management.MIGRATING
    db.upsert_deployment(dep)
    report, managed = await plays.migrate(
        host, dep, new_port=new_port, target_version=target_version, on_step=on_step
    )
    if report.ok:
        managed.status = "healthy"
        db.upsert_deployment(managed)
        dep.status = "stopped"
        db.upsert_deployment(dep)  # keep the adopted record as history, marked stopped
        # TODO(M3): register `managed` with LiteLLM and drop the old registration (litellm_sync).
        return True, managed.id
    dep.management = Management.ADOPTED  # roll back: old server is still the live one
    db.upsert_deployment(dep)
    return False, None


@app.post("/deployments/{dep_id}/migrate")
async def migrate_deployment(dep_id: str, new_port: int, target_version: str = "latest") -> dict:
    """Migrate an adopted server to a standard managed Docker deployment (cutover)."""
    dep, host = _resolve(dep_id)
    ok, managed_id = await _run_migration(dep, host, new_port, target_version)
    return {"ok": ok, "managed_id": managed_id}


@app.post("/deployments/{dep_id}/migrate/stream")
async def migrate_deployment_stream(dep_id: str, new_port: int, target_version: str = "latest") -> StreamingResponse:
    """Same as /migrate, but streams each cutover step as it happens (Server-Sent Events)."""
    dep, host = _resolve(dep_id)

    async def runner(sink: Callable[[dict], None]) -> dict:
        ok, managed_id = await _run_migration(dep, host, new_port, target_version, on_step=sink)
        return {"ok": ok, "managed_id": managed_id}

    return await stream_play(runner)


# -- task ledger -----------------------------------------------------------------
@app.get("/tasks")
def list_tasks(status: str | None = None) -> list[TaskRecord]:
    return db.list_tasks(status)


@app.put("/tasks/{task_id}")
def upsert_task(task_id: str, task: TaskRecord) -> TaskRecord:
    if task.id != task_id:
        raise HTTPException(400, "id mismatch")
    task.updated_at = time.time()
    if not task.started_at:
        task.started_at = task.updated_at
    db.upsert_task(task)
    return task


# -- fleet node registry: register / de-register / list the node file ---------------
class NodeReg(BaseModel):
    name: str
    ip: str
    mac: str
    tier: str
    never_sleep: bool = False
    port: int | None = None
    overwrite: bool = False


@app.get("/nodes")
def list_nodes() -> list[dict]:
    """Nodes in the fleet node file (the /fleet-power inventory). Empty if no file yet."""
    from . import power

    try:
        cfg = power.load_nodes()
    except FileNotFoundError:
        return []
    return [{"name": n, **d} for n, d in sorted((cfg.get("nodes") or {}).items())]


@app.post("/nodes")
def register_node(reg: NodeReg) -> dict:
    """Add or replace a node (name/ip/mac/tier + flags) in the node file, with validation."""
    from . import power

    try:
        path = power.resolve_config_path(create=True)
        return power.register_node(
            path, reg.name, reg.ip, reg.mac, reg.tier,
            never_sleep=reg.never_sleep, port=reg.port, overwrite=reg.overwrite,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.delete("/nodes/{name}")
def deregister_node(name: str) -> dict:
    """Remove a node from the node file. Returns the removed entry."""
    from . import power

    try:
        return power.deregister_node(power.resolve_config_path(), name)
    except FileNotFoundError as e:
        raise HTTPException(503, str(e)) from e
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


# -- fleet power: wake/shutdown a tier (or nodes/ips) and stream progress -----------
@app.get("/power/plan")
def power_plan(
    state: str, tier: str | None = None, nodes: str | None = None,
    ips: str | None = None, all: bool = False, force: bool = False,
) -> dict:
    """Dry-run: resolve the selectors to the exact nodes an action would touch (and what
    it would skip). Lets the Pi command show a confirm-prompt before firing anything."""
    from . import power

    if state not in ("on", "off"):
        raise HTTPException(400, "state must be 'on' or 'off'")
    try:
        cfg = power.load_nodes()
    except FileNotFoundError as e:
        raise HTTPException(503, str(e)) from e
    targets = power.select_targets(cfg, tier=tier, nodes=nodes, ips=ips, all_=all)
    act, skipped = power.partition_never_sleep(targets, force=force) if state == "off" else (targets, [])
    return {
        "state": state,
        "budget_s": power.BUDGET_ON_S if state == "on" else power.BUDGET_OFF_S,
        "nodes": [{"name": n, "ip": d.get("ip"), "tier": d.get("tier")} for n, d in act],
        "skipped": [{"name": n, "ip": d.get("ip"), "reason": "never_sleep"} for n, d in skipped],
        "config": cfg.get("_path"),
    }


@app.get("/power/stream")
async def power_stream(
    state: str, tier: str | None = None, nodes: str | None = None,
    ips: str | None = None, all: bool = False, force: bool = False,
) -> StreamingResponse:
    """Fire the power action and stream real-time SSE: an initial `plan`, per-node phase
    changes (waking→booting→loading→serving, or stopping→offline) with elapsed+ETA,
    `summary` heartbeats, and a final `done`."""
    from . import power

    if state not in ("on", "off"):
        raise HTTPException(400, "state must be 'on' or 'off'")
    try:
        cfg = power.load_nodes()
    except FileNotFoundError as e:
        raise HTTPException(503, str(e)) from e
    targets = power.select_targets(cfg, tier=tier, nodes=nodes, ips=ips, all_=all)

    async def gen() -> AsyncIterator[str]:
        async for event in power.watch(targets, state, cfg, force=force):
            yield _sse(event)

    return StreamingResponse(gen(), media_type="text/event-stream")
