"""HTTP API consumed by the Pi extension (`/fleet`, `/deploy`, `/tasks`) and the harness."""

from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException

from .db import Db
from .models import Deployment, Host, TaskRecord

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
    # TODO(M3): trigger the actual IaC play (plays.deploy) instead of just cataloging,
    # then register the served model with LiteLLM (litellm_sync.register).
    return dep


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
