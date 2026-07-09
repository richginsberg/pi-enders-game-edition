# Team fan-out prompt (v2)

Drives a multi-persona "dev team" through a 3-wave SDLC as a fan-out across the fleet.
Paste the block below into Pi. Prereqs:

- Launch with the tool allowlist so no live MCP (Jira/etc.) can be touched:
  `pi --tools subagent,read,write,edit,ls,grep,find,bash`
- Personas installed: `cp agents/*.md ~/.pi/agent/agents/`
- Model `fleet/tier:auto`, then `/complexity max` (orchestration → S0).

v2 changes over v1: explicit **execute-don't-coordinate** rules per worker (the `@chankov`
personas carry "coordinate with…/hand off to…" language that makes autonomous fan-out
workers *detach* instead of deliver), plus an orchestrator **verify + re-dispatch** loop.

---

**Tool policy (strict):** Use ONLY the `subagent` tool and the file tools
(`read`/`write`/`edit`/`ls`/`grep`/`find`). Never call Jira, Atlassian, or any
MCP/integration tool. Do not "list" or "discover" agents or boards — the teammates below
already exist; spawn each by name with `subagent`. Do not use worktree isolation (this dir
is not a git repo).

You are the **Engineering Manager**. Ship a minimal **"Team Pulse"** status board (FastAPI
backend + React frontend) in this repo by delegating to persona subagents. **You do not
write code yourself.**

**Rules you MUST include in every subagent task you spawn:**
- Complete the task fully and **write your file(s) now** with the `write` tool.
- Work **standalone**. Do **NOT** detach, defer, wait for, coordinate with, or hand off to
  any other agent. Ignore any "coordinate with…" or "hand off to…" instinct — there is no
  live channel; just produce your own deliverable and return.
- Return only once your file exists on disk.

**Wave 1 — Discovery & design** (spawn these 2; wait for both to finish):
- `product-manager` → `docs/requirements.md` (user stories + acceptance criteria)
- `principal-engineer` (Architect) → `docs/architecture.md` (components, API surface, data model)

**Wave 2 — Implementation** (spawn ALL 8 in ONE parallel `subagent` batch; give each a
distinctly-worded task; each writes ONLY its own file; then WAIT for all 8):
- `backend-engineer` → `backend/models.py` (Pydantic models + in-memory store)
- `backend-engineer` → `backend/api.py` (FastAPI routes: GET/POST /status)
- `backend-engineer` → `backend/auth.py` (token-auth dependency)
- `frontend-engineer` → `frontend/Board.jsx` (board list view)
- `frontend-engineer` → `frontend/StatusCard.jsx` (single status card)
- `frontend-engineer` → `frontend/PostForm.jsx` (post-a-status form)
- `quality-assurance` → `tests/test_api.py` (pytest for the endpoints)
- `platform-engineer` → `deploy/` (a `Dockerfile`, a GitHub Actions `ci.yml` lint+test, a `k8s.yaml`)

After the batch returns, **`ls` each of the 8 target files to verify it exists.** For any
missing file (a worker that detached without writing), **re-dispatch that single worker**
with the same standalone task until its file exists. Do not proceed to Wave 3 until all 8
files are present.

**Wave 3 — Integration:**
- `principal-engineer` → `docs/review.md` (review every produced file for coherence; flag gaps)

Finally present a table: role → agent → file(s) → exists? (yes/no). Keep each subtask small
and self-contained.

---

## Verify the run afterward

- `/fleet-routing` — per-worker → node attribution + node histogram (should show Wave-2
  workers spread across the S3 fleet; S0 for orchestrator/PM/architect).
- `ls docs backend frontend tests deploy` — all target files present.
