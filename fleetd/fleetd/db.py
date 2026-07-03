"""SQLite persistence. Documents-in-columns style: each table stores the pydantic
model as JSON plus indexed key columns, so schema evolution stays cheap."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import Deployment, Host, TaskRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS hosts       (id TEXT PRIMARY KEY, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS deployments (id TEXT PRIMARY KEY, host_id TEXT NOT NULL, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks       (id TEXT PRIMARY KEY, status TEXT NOT NULL, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events      (ts REAL NOT NULL, kind TEXT NOT NULL, doc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_deployments_host ON deployments(host_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
"""


class Db:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)

    # -- hosts ---------------------------------------------------------------
    def upsert_host(self, host: Host) -> None:
        self.conn.execute(
            "INSERT INTO hosts(id, doc) VALUES(?, ?) ON CONFLICT(id) DO UPDATE SET doc=excluded.doc",
            (host.id, host.model_dump_json()),
        )
        self.conn.commit()

    def list_hosts(self) -> list[Host]:
        rows = self.conn.execute("SELECT doc FROM hosts").fetchall()
        return [Host(**json.loads(r[0])) for r in rows]

    # -- deployments ----------------------------------------------------------
    def upsert_deployment(self, dep: Deployment) -> None:
        self.conn.execute(
            "INSERT INTO deployments(id, host_id, doc) VALUES(?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET host_id=excluded.host_id, doc=excluded.doc",
            (dep.id, dep.host_id, dep.model_dump_json()),
        )
        self.conn.commit()

    def list_deployments(self, host_id: str | None = None) -> list[Deployment]:
        if host_id:
            rows = self.conn.execute("SELECT doc FROM deployments WHERE host_id=?", (host_id,)).fetchall()
        else:
            rows = self.conn.execute("SELECT doc FROM deployments").fetchall()
        return [Deployment(**json.loads(r[0])) for r in rows]

    # -- tasks ---------------------------------------------------------------
    def upsert_task(self, task: TaskRecord) -> None:
        self.conn.execute(
            "INSERT INTO tasks(id, status, doc) VALUES(?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, doc=excluded.doc",
            (task.id, task.status, task.model_dump_json()),
        )
        self.conn.commit()

    def list_tasks(self, status: str | None = None) -> list[TaskRecord]:
        if status:
            rows = self.conn.execute("SELECT doc FROM tasks WHERE status=?", (status,)).fetchall()
        else:
            rows = self.conn.execute("SELECT doc FROM tasks").fetchall()
        return [TaskRecord(**json.loads(r[0])) for r in rows]
