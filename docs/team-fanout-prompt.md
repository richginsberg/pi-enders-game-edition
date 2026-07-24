# Team fan-out prompt (v3)

Drives a multi-persona "dev team" through a 3-wave SDLC as a fan-out across the fleet.
Paste the block below into Pi. Prereqs:

- Launch with the tool allowlist so no live MCP (Jira/etc.) can be touched:
  `pi --tools subagent,read,write,edit,ls,grep,find,bash`
- Personas installed: `cp agents/*.md ~/.pi/agent/agents/`
- Model `fleet/tier:auto`, then `/complexity max` (orchestration → S0).
- **Write your `IDEA.md` first** (repo root of the target repo, e.g. `~/code/testrepo/IDEA.md`).
  This is the seed: a short brief in the voice of a **customer or stakeholder** — the problem
  they have, who they are, what they wish existed, any constraints, and what success looks
  like. Deliberately *not* a spec: no file names, no tech choices, no API design. The
  **Product Manager** and **Architect** turn it into real requirements in Wave 1. A sample
  template is at the bottom of this file — copy it and rewrite in your own words, or start
  from a ready-made seed in [`docs/ideas/`](ideas/README.md) (zero-to-one, iterate-on-mature,
  tech-debt, bug-fix, port-to-new-stack, accessibility, performance) e.g.
  `cp docs/ideas/zero-to-one.example.md ~/code/testrepo/IDEA.md`.

v3 changes over v2: the run is now **idea-driven**. Instead of a hardcoded product, Wave 1
sources `IDEA.md` (customer/stakeholder perspective); the `product-manager` and
`principal-engineer` (Architect) flesh it out into requirements + architecture, and the
orchestrator derives the Wave-2 build from *their* file plan. v2's execute-don't-coordinate
worker rules and the orchestrator verify + re-dispatch loop are unchanged.

---

**Tool policy (strict):** Use ONLY the `subagent` tool and the file tools
(`read`/`write`/`edit`/`ls`/`grep`/`find`). Never call Jira, Atlassian, or any
MCP/integration tool. Do not "list" or "discover" agents or boards — the teammates below
already exist; spawn each by name with `subagent`. Do not use worktree isolation (this dir
is not a git repo).

**How to dispatch (tool contract — follow exactly):** There is ONE dispatch tool named
`subagent`. There is NO `task` tool and NO `agent` tool — never call those. To run several
agents **in parallel**, make a SINGLE `subagent` call with a `tasks` array:
`subagent({ tasks: [ {agent: "backend-engineer", task: "..."}, {agent: "frontend-engineer", task: "..."}, ... ] })`.
**Always use the `tasks` array form — even for a single agent:**
`subagent({ tasks: [ { agent: "principal-engineer", task: "..." } ] })`. Do NOT use the
single `subagent({ agent, task })` form — it errors with "Provide exactly one mode". Put
EVERY parallel worker in the one `tasks` array; do not make separate calls per worker.

**Act, don't announce:** never end a turn with only a plan or a statement like "now
spawning the workers." Whenever you say you will dispatch agents, **emit the `subagent`
tool call in that SAME message.** A turn that describes the next step without making the
tool call is a failure — keep going until each wave's `subagent` call has actually been
issued.

You are the **Engineering Manager**. Ship the product described in **`IDEA.md`** in this
repo by delegating to persona subagents. **You do not write code yourself.** Before Wave 1,
**read `IDEA.md`** so you understand the customer's intent — but do NOT expand it into a
spec yourself; that is the PM's and Architect's job. If `IDEA.md` is missing, stop and say
so rather than inventing a product.

**Rules you MUST include in every subagent task you spawn:**
- Complete the task fully and **write your file(s) now** with the `write` tool.
- **Write to the EXACT repo-relative path given** (e.g. `backend/api.py` under the repo
  root, `~/code/testrepo`). Do NOT invent subdirectories (no `test4/…`, no `src/…`), rename,
  or write anywhere else. Getting the path wrong = the file is missing.
- Work **standalone**. Do **NOT** detach, defer, wait for, coordinate with, or hand off to
  any other agent. Ignore any "coordinate with…" or "hand off to…" instinct — there is no
  live channel; just produce your own deliverable and return.
- **Return a one-line confirmation of the absolute path you wrote** (e.g.
  `wrote /home/…/testrepo/backend/api.py`) so the orchestrator can verify without guessing.
- Return only once your file exists on disk at that exact path.

**Wave 1 — Discovery & design** (ONE parallel `subagent` call with a 2-entry `tasks` array).
Both workers must **`read` `IDEA.md` first** and build directly off it:
- `product-manager` → `docs/requirements.md`: translate the stakeholder's `IDEA.md` into
  user stories + acceptance criteria. Fill gaps the customer left implicit with reasonable
  assumptions, and list those assumptions explicitly. Scope to a minimal first version.
- `principal-engineer` (Architect) → `docs/architecture.md`: read `IDEA.md` **and**
  `docs/requirements.md`, then design components, API surface, and data model. **End with a
  "File plan" section**: an explicit list of the source files to build (repo-relative path →
  one-line purpose → which persona should own it: backend-engineer / frontend-engineer /
  quality-assurance / platform-engineer). The orchestrator dispatches Wave 2 straight from
  this list, so make it concrete (aim for ~6–10 files).

After Wave 1 returns, **`read` `docs/architecture.md`** and use its **File plan** as the
Wave-2 work list.

**Wave 2 — Implementation** (spawn EVERY file in the architecture's File plan in ONE
parallel `subagent` batch; give each a distinctly-worded task; each writes ONLY its own
file; then WAIT for all of them). Map each File-plan entry to its named persona. A typical
plan for a small web app looks like the reference below (yours comes from `IDEA.md`, so it
will differ):
- `backend-engineer` → `backend/models.py` (data models + store)
- `backend-engineer` → `backend/api.py` (API routes)
- `backend-engineer` → `backend/auth.py` (auth dependency)
- `frontend-engineer` → `frontend/<View>.jsx` (main view)
- `frontend-engineer` → `frontend/<Item>.jsx` (item component)
- `frontend-engineer` → `frontend/<Form>.jsx` (input form)
- `quality-assurance` → `tests/test_api.py` (pytest for the endpoints)
- `platform-engineer` → `deploy/` (a `Dockerfile`, a GitHub Actions `ci.yml` lint+test, a `k8s.yaml`)

After the batch returns, **verify the File-plan targets with ONE bash command** (e.g.
`ls backend/*.py frontend/*.jsx tests/*.py deploy/*` from the repo root, adjusted to the
plan's paths) — do NOT make multiple `ls` tool calls (they error with "path Tool not
found"). For any missing file (a worker that detached without writing), **re-dispatch those
missing workers ONE more time** in a single parallel `subagent` batch, adding "write the
file now, do not detach." Do this re-dispatch **at most once** — then proceed to Wave 3 and
note any still-missing file as a gap. (Do NOT loop re-dispatching the same worker repeatedly
— that spins the orchestrator without progress.)

**Wave 3 — Integration:**
- `principal-engineer` → `docs/review.md` (review every produced file against `IDEA.md` and
  `docs/requirements.md` for coherence; flag gaps and unmet acceptance criteria)

Finally present a table: role → agent → file(s) → exists? (yes/no). Keep each subtask small
and self-contained.

---

## Verify the run afterward

- `/fleet-routing` — per-worker → node attribution + node histogram (should show Wave-2
  workers spread across the S3 fleet; S0 for orchestrator/PM/architect).
- `ls docs backend frontend tests deploy` — all target files present.

---

## Sample `IDEA.md` (copy to the target repo root, rewrite in the customer's voice)

Keep it in plain stakeholder language. Say what you want and why — **not** how to build it.

```markdown
# IDEA: <one-line title of what I wish existed>

## Who I am
<e.g. "I run ops for a 30-person consultancy." Your role, your team, your context.>

## The problem
<What's painful today, in your words. What you do now and why it doesn't work.>

## What I wish existed
<The thing you want, described by what it does for you — not its screens or tech.
 "I want to see, at a glance, what everyone is working on and whether they're blocked."">

## Who would use it
<The people who'd touch it and roughly what each of them needs to do.>

## Constraints & must-haves
<Anything non-negotiable: has to be simple, has to work on our phones, no new logins,
 keep it internal, budget/time limits, data that must stay private, etc.>

## What success looks like
<How you'll know it worked. "In week one, the whole team posts a daily status without me
 chasing them." Concrete, observable outcomes — these become acceptance criteria.>

## Out of scope (for now)
<Things you explicitly do NOT want in the first version, so the team doesn't gold-plate.>
```
