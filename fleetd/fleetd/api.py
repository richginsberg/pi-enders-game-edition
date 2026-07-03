"""HTTP API consumed by the Pi extension (`/fleet`, `/deploy`, `/tasks`) and the harness."""

from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException

from .db import Db
from .models import Deployment, Host, Management, TaskRecord

DB_PATH = Path(os.environ.get("FLEETD_DB", "~/.local/share/fleetd/fleet.sqlite3")).expanduser()

app = FastAPI(title="fleetd", version="0.1.0")
db = Db(DB_PATH)


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

    dep = next((d for d in db.list_deployments() if d.id == dep_id), None)
    if dep is None:
        raise HTTPException(404, f"unknown deployment {dep_id}")
    host = next((h for h in db.list_hosts() if h.id == dep.host_id), None)
    if host is None:
        raise HTTPException(409, f"deployment references unknown host {dep.host_id}")

    dep.status = "deploying"
    db.upsert_deployment(dep)
    report = await plays.deploy(host, dep)
    dep.status = "healthy" if report.ok else "unhealthy"
    db.upsert_deployment(dep)
    # TODO(task #8/#10): on success register the model with LiteLLM (litellm_sync).
    return {"ok": report.ok, "steps": report.steps}


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

    dep = next((d for d in db.list_deployments() if d.id == dep_id), None)
    if dep is None:
        raise HTTPException(404, f"unknown deployment {dep_id}")
    host = next((h for h in db.list_hosts() if h.id == dep.host_id), None)
    if host is None:
        raise HTTPException(409, f"deployment references unknown host {dep.host_id}")
    proposed = plays.plan_migration(dep, new_port=new_port, target_version=target_version)
    return {"proposed": proposed.model_dump(), "diff": plays.migration_diff(host, dep, proposed)}


@app.post("/deployments/{dep_id}/migrate")
async def migrate_deployment(dep_id: str, new_port: int, target_version: str = "latest") -> dict:
    """Migrate an adopted server to a standard managed Docker deployment (cutover)."""
    from . import plays

    dep = next((d for d in db.list_deployments() if d.id == dep_id), None)
    if dep is None:
        raise HTTPException(404, f"unknown deployment {dep_id}")
    host = next((h for h in db.list_hosts() if h.id == dep.host_id), None)
    if host is None:
        raise HTTPException(409, f"deployment references unknown host {dep.host_id}")

    dep.management = Management.MIGRATING
    db.upsert_deployment(dep)
    report, managed = await plays.migrate(host, dep, new_port=new_port, target_version=target_version)
    if report.ok:
        managed.status = "healthy"
        db.upsert_deployment(managed)
        dep.status = "stopped"
        db.upsert_deployment(dep)  # keep the adopted record as history, marked stopped
        # TODO(M3): register `managed` with LiteLLM and drop the old registration (litellm_sync).
    else:
        dep.management = Management.ADOPTED  # roll back: old server is still the live one
        db.upsert_deployment(dep)
    return {"ok": report.ok, "steps": report.steps, "managed_id": managed.id if report.ok else None}


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
