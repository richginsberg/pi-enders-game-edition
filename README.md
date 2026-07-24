# Divide and Conquer

Fleet-scale AI coding harness built on [Pi](https://pi.dev): orchestrates squads of
heterogeneous GPU inference hosts (Pascal → Volta → Ampere → frontier APIs) with
complexity-tiered, KV-cache-affinity routing, relentless task completion, and
TUI-driven fleet governance. See [PLAN.md](PLAN.md) for the full design.

## Status

| Milestone | State |
|---|---|
| **M1 — Tiered routing** | ✅ measured (`tools/m1_benchmark.py`): `tier:auto` routes by complexity; repeated-prefix cache-hit 0%→99% cold-to-warm. Cross-host affinity A/B deferred until a squad has ≥2 nodes. |
| **M2 — Ralph loops + DoD + judge** | ✅ implemented — [`harness/`](harness/) (`ralph`, `dod`, `fanout`) + `/tasks` checklist with tmux peek. |
| **M3 — Fleet IaC + deploy wizard** | ✅ — fleetd plays (deploy/upgrade/**adopt**/**migrate**), SSH discovery, `/deploy` wizard, BC-250 Vulkan container. |
| **M4 — Observability + governance** | ◻ pending (no Prometheus/Grafana yet). Foothold in place: LiteLLM DB-backed UI + spend/logs, and gateway health-check/cooldown/failover. |
| **M5 — Personas + long-term context** | ✅ — @chankov personas + PM/Principal; pgvector store, `embed:qwen3`, vague-prompt recall/inject, salience-judge writes. |

**Next:** bring **S2 (32 GB card)** and additional **BC-250 (S3)** nodes online and register them — unblocks the M1 cross-host affinity A/B and real gateway failover (which both need ≥2 nodes per squad).

## Layout

| Directory | Language | Purpose |
|---|---|---|
| [`fleetd/`](fleetd/) | Python | Sidecar daemon: host inventory, SSH/Docker IaC plays, health polling, task ledger, LiteLLM registration |
| [`router/`](router/) | Python | LiteLLM proxy config + custom routing strategy (tier selection, prefix-hash cache affinity) |
| [`cliproxy/`](cliproxy/) | Python | Pluggable OpenAI-compatible auth-bridge: presents `/v1` outward, adapts CLI/provider auth (e.g. Grok) inward |
| [`pi-ext/`](pi-ext/) | TypeScript | Pi extension: fleet provider (models from LiteLLM), `/fleet`, `/deploy`, `/tasks` commands |
| [`harness/`](harness/) | TypeScript | Relentless (Ralph-loop) runner: DoD ledger, judge/enlistment, subagent fan-out |
| [`context/`](context/) | Python | Long-term context: repo-partitioned pgvector store + `embed:qwen3` client; `remember()`/`recall()` for the RAG layer |
| [`agents/`](agents/) | Markdown | Engineering personas (Product Manager, Principal Engineer) added on top of `@chankov/agent-skills`, bound to fleet tiers |
| [`tools/`](tools/) | Python | Operational scripts (e.g. `bench_embed.py` — embedding-endpoint latency benchmark) |
| [`deploy/`](deploy/) | Bash/systemd | Control-plane IaC: `bootstrap.sh`, systemd units, embedding installer, and the [standup runbook](deploy/README.md) |
| `pi/` | — | Upstream Pi clone, reference only (not part of this repo) |

## Quick start (M1)

```bash
# 1. Control-plane box: LiteLLM with the custom router
cd router && pip install -e .
cp litellm-config.example.yaml litellm-config.yaml  # local copy, gitignored — add your endpoints/keys

# Serve it. IMPORTANT: `tier:auto` (complexity tiers + prefix-hash affinity) is a custom
# routing strategy the proxy YAML can't register — use the launcher, not plain `litellm`:
python -m dnc_router.serve --config litellm-config.yaml --host 0.0.0.0 --port 4000
#   (plain `litellm --config …` also works but only routes the explicit tiers tier:s0..s3)

# 2. fleetd
cd fleetd && pip install -e . && fleetd serve

# 3. Pi extension (symlink into Pi's extension dir)
cd pi-ext && npm install
ln -s "$(pwd)" ~/.pi/agent/extensions/divide-and-conquer
```

### Optional: admin UI, spend tracking & gateway auto-heal

The gateway routes fine without a database. The LiteLLM **web UI** (login, virtual keys,
spend/logs) needs Postgres — set `DATABASE_URL` in your env and the launcher bootstraps
Prisma (client + schema) automatically on startup:

```bash
createdb litellm                                     # reuse the pgvector Postgres, separate DB
export DATABASE_URL=postgresql://<user>:<pass>@127.0.0.1:5432/litellm
python -m dnc_router.serve --config litellm-config.yaml --host 0.0.0.0 --port 4000
#   → UI at http://<control-plane>:4000/ui  (login: admin + your LITELLM_MASTER_KEY)
```

**Auto-heal (LB-style):** `router_settings` in the config sets health-check / cooldown /
retry / fallback; the custom strategy skips cooling-down and at-capacity (single-slot)
nodes and rehashes their traffic to a live sibling. A downed host fails the fast `/health`
probe; a slow generation keeps the probe green (it stays 200 through a multi-minute
prefill) — so the two are never confused. **Sideways failover needs ≥2 nodes registered
under a tier** (else it falls up a tier); register each BC-250 as its own `tier:s3` entry.

## Fleet operations (M3)

fleetd manages inference servers over SSH — deploy new ones, or **adopt** servers it didn't
create (discover a running llama.cpp/vLLM, catalog + register with LiteLLM, monitor without
touching its lifecycle) and optionally **migrate** them to a standard managed container:

```bash
fleetd discover <host>          # inspect a host's running inference server(s)
fleetd deploy   <host> …        # idempotent play: pull image, render config, start, health, register
# adopt / migrate: see fleetd/fleetd/plays.py + the /deploy TUI wizard in pi-ext
```

**BC-250 S3 nodes** (24× AMD, Vulkan/RADV — no ROCm) have their own container + serving
guide, including the dynamic-VRAM BIOS split and the `--jinja`/generation-cap gotchas for
reasoning models: see [`deploy/bc250/README.md`](deploy/bc250/README.md). Model-selection
and VRAM math for the S2/S3 tiers: [`docs/model-selection-2026.md`](docs/model-selection-2026.md).

## Fleet power & node registry

The fleet is mostly powered off; nodes are woken on demand and put back to sleep. All of
this is driven from Pi via `/fleet-power` (fleetd does the work; the extension streams
progress over SSE), with `tools/fleetpower.py` as a standalone CLI fallback.

```
/fleet-power s3 on            wake tier S3, watch each node reach *serving* (live ETA)
/fleet-power s3 off           graceful shutdown (confirms; skips never_sleep nodes)
/fleet-power all on | .106 on | 1,2,3 on
/fleet-power list             the node registry
/fleet-power register bc25007 192.168.1.124 <mac> s3 chassis=c1 [never_sleep] [port=N]
/fleet-power deregister bc25007
/fleet-power litellm-sync     regenerate the gateway's S3 entries from the registry + restart
```

- **Real serving proof, not just "up":** each node walks a state machine —
  `waking → booting → loading → serving` (a real `/health` 200, not a port ping) — with
  elapsed + ETA per node. OFF walks `stopping → offline`.
- **One node registry, three consumers.** `~/dnc/fleet-nodes.yaml` (name → ip/mac/tier +
  `chassis`/`never_sleep`/`port`) is the source of truth. fleetd manages it via
  `register`/`deregister`; `fleetpower.py --sync` pulls it down to the local fallback; and
  `litellm-sync` regenerates the **gateway's** per-node routing entries from it (marker-fenced
  so hand-managed `tier:s0`/`tier:s1` entries stay untouched). A rebuilt node's new DHCP IP
  follows everywhere from one `register … overwrite` — the MAC (the WoL identity) is stable.
- **Chassis-aware ordering.** The per-chassis fan-controller node (`never_sleep`, same
  `chassis` id as its mates) is **first-to-wake** (mates gate until it's reachable — they're
  `blocked` if it never comes up) and **last-to-sleep** on a forced OFF, so boards never run
  without cooling. Nodes with no `chassis` (single/multi-GPU S1/S2 boxes) power in parallel.

The control-plane services (`dnc-litellm`, `dnc-fleetd`, `dnc-context`) run as systemd units
that auto-start on boot and self-heal on crash — see [`deploy/README.md`](deploy/README.md).

## Engineering personas (M5)

We adopt [`@chankov/agent-skills`](https://pi.dev/packages/@chankov/agent-skills)
(15 personas with per-persona model switching) rather than build our own, and extend it:

```bash
npx @chankov/agent-skills init        # installs the 15 stock personas into agents/
```

- **Added roles**: [Product Manager](agents/product-manager.md) and
  [Principal Engineer](agents/principal-engineer.md) — the two the package lacks —
  bound to the largest fleet models (`fleet/tier:s0`/`tier:s1`).
- **Tier bindings**: [`.ai/agent-skills-overrides.md`](.ai/agent-skills-overrides.md)
  maps every persona's model to a fleet squad tier. Referencing a `fleet/tier:*`
  model means persona subagents route through the custom router (complexity + cache
  affinity) just like interactive sessions.

Each persona is a Markdown agent definition (name/description/model frontmatter). The
`model:` field binds it to a squad — heavy design/review roles pin to the frontier
(`s0`/`s1`), routine build/test roles to `s2`, docs to the wide `s3` fleet, and a few
stay on `tier:auto` for per-turn complexity routing:

| Persona | Bound tier | File |
|---|---|---|
| Principal Engineer | `fleet/tier:s0` | [principal-engineer.md](agents/principal-engineer.md) |
| Product Manager | `fleet/tier:s0` | [product-manager.md](agents/product-manager.md) |
| Engineering Manager | `fleet/tier:s1` | [engineering-manager.md](agents/engineering-manager.md) |
| ML / Inference Engineer | `fleet/tier:s1` | [ml-inference-engineer.md](agents/ml-inference-engineer.md) |
| Build / Release Manager | `fleet/tier:s2` | [build-release-manager.md](agents/build-release-manager.md) |
| Designer | `fleet/tier:s2` | [designer.md](agents/designer.md) |
| Observability Engineer | `fleet/tier:s2` | [observability-engineer.md](agents/observability-engineer.md) |
| Platform Engineer | `fleet/tier:s2` | [platform-engineer.md](agents/platform-engineer.md) |
| Quality Assurance | `fleet/tier:s2` | [quality-assurance.md](agents/quality-assurance.md) |
| Technical Writer | `fleet/tier:s3` | [technical-writer.md](agents/technical-writer.md) |
| Backend Engineer | `fleet/tier:auto` | [backend-engineer.md](agents/backend-engineer.md) |
| Data Engineer | `fleet/tier:auto` | [data-engineer.md](agents/data-engineer.md) |
| Frontend Engineer | `fleet/tier:auto` | [frontend-engineer.md](agents/frontend-engineer.md) |
| SRE | `fleet/tier:auto` | [sre.md](agents/sre.md) |

- **Making them executable**: Pi discovers spawnable agents from `~/.pi/agent/agents/`
  (global) and `<repo>/.pi/agents/` (project). Install with
  `cp agents/*.md ~/.pi/agent/agents/` so they appear in the subagent list alongside
  Pi's built-ins (`worker`, `planner`, …) in any session.

## Long-term context (M5)

Shared semantic memory, partitioned by repo, in Postgres + pgvector. Durable facts
are embedded via the fleet's `embed:qwen3` and retrieved to enrich vague prompts.

```bash
cd context && pip install -e .
cp .env.example .env               # set DNC_PG_DSN + embedding endpoint (gitignored)
python -m context.cli partition    # show the detected repo partition key
python -m context.cli recall "how does the router pick a squad?"
python -m context.cli serve        # HTTP sidecar on :7432 (POST /recall, /remember)
```

`ContextService.remember(items)` / `recall(query)` are the API the salience-judge
write path and vague-prompt injection build on. Requires pgvector ≥ 0.5 (HNSW index).

**Vague-prompt injection**: the Pi extension watches `before_agent_start`; when a
prompt is short/under-specified it calls the sidecar's `/recall` (passing the session
cwd for partition resolution), injects the top matches into the system prompt, and
shows a `pulled N context items` status. Override the sidecar URL with
`DNC_CONTEXT_URL`. If the sidecar is down, the turn proceeds uninjected.

**Salience-judge writes**: at the end of each response (`agent_end`, debounced) and
on demand via `/remember`, the extension posts the new transcript to `/distill`. A
cheap S3 judge (`DNC_JUDGE_MODEL`) extracts only durable facts — decisions,
constraints, outcomes, handoffs — which are embedded and written to the repo
partition. Chatter and low-value turns store nothing.
