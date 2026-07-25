# Divide and Conquer

**Stop paying frontier prices for boilerplate.**

Divide and Conquer is a complexity-tiered router for AI coding agents. It grades each
request, sends it to the cheapest model that can actually do the job, and pins a
conversation to one backend so its KV-cache stays warm. Built on [Pi](https://pi.dev),
served through [LiteLLM](https://litellm.ai) as one OpenAI-compatible endpoint.

The problem it solves: coding agents default to *one* model for everything — architecture
decisions and `.gitignore` edits alike. Most of your tokens are low-complexity work paying
top-tier rates. Tiering that work is worth **5–10×** on a typical month
([worked example](docs/cloud-tiers.md#what-it-saves)).

```
                      ┌── S0  max     frontier reasoning   ~$5/$25 per Mtok
  request ─ grade ────┼── S1  high    implementation       ~$2/$10
   complexity         ├── S2  medium  routine code, tests  ~$0.09/$0.19
                      └── S3  low     docs, bulk, judge    ~$0.04/$0.17   (or free)
```

## Two ways to run it

The routing engine is **provider-agnostic**: it maps complexity → a squad tag
(`dnc_squad`) → a deployment's `api_base`. It neither knows nor cares whether that
deployment is a hosted API or a box on your LAN. So the two modes are the *same software
with a different config file*.

| | **Cloud-only** (default) | **Hybrid** |
|---|---|---|
| You need | API keys. That's it. | Keys + your own GPU hosts |
| Install | Pi, pi-ext, LiteLLM + router | …plus `fleetd` and node provisioning |
| Tiers are | 4 price points of hosted API | frontier API on top, your hardware below |
| Marginal cost | per token | electricity |
| Best for | **almost everyone** | you already own idle GPUs |
| Start here | [Cloud-only setup](#cloud-only-setup-default) | [Hybrid setup](#hybrid-setup-add-your-own-gpus) |

Cloud-only is the default because it needs no hardware and delivers the entire thesis. The
hybrid path exists because once tiering works, *owning* the bottom tiers makes the bulk
work nearly free — see the [BC-250 fleet](deploy/bc250/README.md) (~$50/board, ~56 tok/s
on a 35B reasoning model).

## Status

| Milestone | State |
|---|---|
| **M1 — Tiered routing** | ✅ measured (`tools/m1_benchmark.py`): `tier:auto` routes by complexity; repeated-prefix cache-hit 0%→99% cold-to-warm. |
| **M2 — Ralph loops + DoD + judge** | ✅ implemented — [`harness/`](harness/) (`ralph`, `dod`, `fanout`) + `/tasks` checklist with tmux peek. |
| **M3 — Fleet IaC + deploy wizard** | ✅ — fleetd plays (deploy/upgrade/**adopt**/**migrate**), SSH discovery, `/deploy` wizard, BC-250 Vulkan container. |
| **M4 — Observability + governance** | ◻ pending (no Prometheus/Grafana yet). Foothold: LiteLLM DB-backed UI + spend/logs, gateway health-check/cooldown/failover. |
| **M5 — Personas + long-term context** | ✅ — @chankov personas + PM/Principal; pgvector store, `embed:qwen3`, vague-prompt recall/inject, salience-judge writes. |
| **Cloud-only mode** | ✅ config + [tier reference](docs/cloud-tiers.md). Same router, no hardware. |
| **Burst GPU (rented, on demand)** | ◻ designed, not built — [`docs/burst-gpu.md`](docs/burst-gpu.md) |

## Layout

| Directory | Language | Purpose |
|---|---|---|
| [`router/`](router/) | Python | LiteLLM proxy config + custom routing strategy (tier selection, prefix-hash cache affinity) |
| [`pi-ext/`](pi-ext/) | TypeScript | Pi extension: fleet provider (models from LiteLLM), `/fleet`, `/fleet-power`, `/deploy`, `/tasks` |
| [`fleetd/`](fleetd/) | Python | *(hybrid)* Sidecar daemon: host inventory, SSH/Docker IaC plays, power control, health polling, LiteLLM registration |
| [`cliproxy/`](cliproxy/) | Python | Pluggable OpenAI-compatible auth-bridge: presents `/v1` outward, adapts CLI/provider auth inward |
| [`harness/`](harness/) | TypeScript | Relentless (Ralph-loop) runner: DoD ledger, judge/enlistment, subagent fan-out |
| [`context/`](context/) | Python | Long-term context: repo-partitioned pgvector store + `embed:qwen3` client |
| [`agents/`](agents/) | Markdown | Engineering personas bound to fleet tiers |
| [`tools/`](tools/) | Python | Operational scripts (`fleetpower.py`, benchmarks) |
| [`deploy/`](deploy/) | Bash/systemd | Control-plane IaC: `bootstrap.sh`, systemd units, [standup runbook](deploy/README.md) |
| [`docs/`](docs/) | Markdown | [Cloud tiers & pricing](docs/cloud-tiers.md), [burst GPU](docs/burst-gpu.md), [team fan-out](docs/team-fanout-prompt.md), [idea seeds](docs/ideas/README.md) |

---

# Cloud-only setup (default)

No hardware. Three components: Pi (the agent), the Pi extension (fleet-aware UX), and
LiteLLM + the custom router (the tiering brain). `fleetd` is **not needed** — it exists to
manage machines, and in this mode there are none.

```bash
# 1. Router + gateway
cd router && pip install -e .
cp litellm-config.cloud.example.yaml litellm-config.yaml   # gitignored; edit freely
export OPENROUTER_API_KEY=sk-or-...          # the only key strictly required
export LITELLM_MASTER_KEY=$(openssl rand -base64 32)

# IMPORTANT: use the launcher, not plain `litellm`. `tier:auto` (complexity tiers +
# prefix-hash affinity) is a custom routing strategy the proxy YAML cannot register.
python -m dnc_router.serve --config litellm-config.yaml --host 0.0.0.0 --port 4000

# 2. Pi extension
cd ../pi-ext && npm install
ln -s "$(pwd)" ~/.pi/agent/extensions/divide-and-conquer
export DNC_LITELLM_URL=http://localhost:4000

# 3. Verify
curl -s localhost:4000/v1/models -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

In Pi, pick model `fleet/tier:auto` and drive the tier with `/complexity low|medium|high|max`,
or pin a tier directly with `fleet/tier:s2`. The response footer shows which tier actually
answered (`x-dnc-squad`).

For durability (systemd units that survive reboot), see [`deploy/README.md`](deploy/README.md) —
in cloud-only mode you only need the `dnc-litellm` unit.

## The tier ladder

Defaults shipped in [`litellm-config.cloud.example.yaml`](router/litellm-config.cloud.example.yaml).
Prices $ per million tokens, **verified 2026-07-25 — they drift**.

| Tier | Complexity | Default | in | out | Job |
|---|---|---|---|---|---|
| **S0** | `max` | `anthropic/claude-opus-5` | 5.00 | 25.00 | Architecture, hard debugging, review |
| **S1** | `high` | `anthropic/claude-sonnet-5` | 2.00 | 10.00 | Feature implementation, refactors |
| **S2** | `medium` | `deepseek/deepseek-v4-flash` | 0.094 | 0.188 | Routine code, tests |
| **S3** | `low` | `openai/gpt-oss-120b` | 0.037 | 0.170 | Docs, formatting, judge, bulk |

**S0 → S3 is ~135× on input, ~147× on output.** On an illustrative 10M-in/2M-out month, a
tiered mix costs **$18.74** vs **$100** all-Opus-5 — and the bottom two tiers do **70% of
the work for under 4% of the bill**.

Alternatives for every tier (including **free** S3 models), provider-by-provider price
arbitrage, throughput notes, and OpenRouter's `max_price` / quantization / ZDR routing
controls: **[`docs/cloud-tiers.md`](docs/cloud-tiers.md)**.

Swapping a tier is a one-line edit — change the `model:` on that deployment. Each tier
registers **two** deployments so failover moves sideways rather than falling up a tier.

## Registering providers

Any OpenAI-compatible `/v1` endpoint is a valid deployment: add a `model_list` entry, tag
it `model_info.dnc_squad: s0|s1|s2|s3`, and the router picks it up. Verified base URLs for
OpenRouter, DeepInfra, Groq, Together, Fireworks, Novita and Cerebras are listed in
[`docs/cloud-tiers.md`](docs/cloud-tiers.md#provider-notes).

Two patterns worth knowing:

- **Buy open weights on OpenRouter.** For identical open-weight models it was cheapest in
  nearly every comparison — `gpt-oss-120b` at $0.037/$0.17 vs $0.15/$0.60 on Together,
  Fireworks *and* Groq. A 4× spread for the same weights.
- **Buy latency deliberately.** Groq is the only provider publishing throughput
  (~1,000 tok/s on `gpt-oss-20b`) at ~2.5× the price. Register it as a *latency override*,
  not the bulk default.

### On subscriptions

If you're here because a max-tier subscription is being burned on low-complexity work: the
cleanest fix is to **leave the subscription in your interactive editor** and point this
gateway at pay-per-token endpoints for everything the agent does autonomously. That's the
config above, and it requires no bridging.

Routing a subscription *through* the gateway as S0 is harder — those plans authenticate via
their vendor's CLI, not an API key, so it needs an auth bridge ([`cliproxy/`](cliproxy/)).
Be warned: our own attempt to bridge a Grok CLI subscription **failed** (the binary resists
interception) and we fell back to a pay-per-token key — and provider terms may not permit
it. Opt-in and yours to validate; see
[the caveats](docs/cloud-tiers.md#on-subscriptions-as-the-s0-tier).

## Burst GPU (designed, not built)

Renting a GPU on demand from RunPod/Vast.ai, deploying a model server, registering it as a
tier, and destroying it when idle — reusing the same registry → `litellm-sync` plumbing the
physical nodes use.

**The research says do this narrowly.** Break-even is
`sustained tok/s = hourly_rate ÷ price_per_output_token`, and the cheap hosted tiers are so
cheap that undercutting `gpt-oss-120b` ($0.17/M out) means holding **~400–2,300 tok/s every
hour you pay for**. You can't beat S2/S3 by renting. You *can* trivially beat S0/S1
(~39 tok/s on an A100 to undercut Sonnet 5) — but only if an open model that fits actually
does the job you were paying S1 for.

So it's worth it for **saturated fan-out bursts**, work you can't send to an API, models no
API hosts, or sustained S1-quality volume — and not otherwise. Full provider comparison,
break-even table, driver design, and the six things that will bite (starting with: an
orphaned 4090 pod is **~$17/day**): **[`docs/burst-gpu.md`](docs/burst-gpu.md)**.

---

# Hybrid setup (add your own GPUs)

Everything above, plus locally-hosted tiers. Frontier stays a hosted API (S0); S1–S3 move
onto hardware you own, where the marginal cost of a token is electricity. This is the
original design — [`router/litellm-config.example.yaml`](router/litellm-config.example.yaml)
is the self-hosted config template.

```bash
# In addition to the cloud-only steps:
cd fleetd && pip install -e . && fleetd serve      # host inventory, IaC plays, power control
```

## Fleet operations

fleetd manages inference servers over SSH — deploy new ones, or **adopt** servers it didn't
create (discover a running llama.cpp/vLLM, catalog + register with LiteLLM, monitor without
touching its lifecycle) and optionally **migrate** them to a standard managed container:

```bash
fleetd discover <host>          # inspect a host's running inference server(s)
fleetd deploy   <host> …        # idempotent play: pull image, render config, start, health, register
# adopt / migrate: see fleetd/fleetd/plays.py + the /deploy TUI wizard in pi-ext
```

Deploys are gated on **GPU-bound inference**, not just a green health check: the play fires
one real completion and fails the node if it's serving CPU-bound. This catches the nastiest
failure mode we hit — a node that health-checks green while silently running inference on
the CPU at ~1/5 speed, because the kernel never gave the container a usable GPU compute
device. A node like that now fails provisioning instead of quietly joining the fleet.

**BC-250 S3 nodes** (24× AMD, Vulkan/RADV — no ROCm) have their own container + serving
guide, including the dynamic-VRAM BIOS split and the `--jinja`/generation-cap gotchas for
reasoning models: see [`deploy/bc250/README.md`](deploy/bc250/README.md). Model-selection
and VRAM math for self-hosted tiers: [`docs/model-selection-2026.md`](docs/model-selection-2026.md).

## Fleet power & node registry

The fleet is mostly powered off; nodes are woken on demand and put back to sleep. Driven
from Pi via `/fleet-power` (fleetd does the work; the extension streams progress over SSE),
with [`tools/fleetpower.py`](tools/fleetpower.py) as a standalone CLI fallback.

```
/fleet-power s3 on            wake tier S3, watch each node reach *serving* (live ETA)
/fleet-power s3 off           graceful shutdown (confirms; skips never_sleep nodes)
/fleet-power all on | .106 on | 1,2,3 on
/fleet-power list             the node registry
/fleet-power register bc25007 <ip> <mac> s3 chassis=c1 [never_sleep] [port=N]
/fleet-power deregister bc25007
/fleet-power litellm-sync     regenerate the gateway's S3 entries from the registry + restart
```

- **Real serving proof, not just "up":** each node walks a state machine —
  `waking → booting → loading → serving` (a real `/health` 200, not a port ping) — with
  elapsed + ETA per node. OFF walks `stopping → offline`.
- **One node registry, three consumers.** `~/dnc/fleet-nodes.yaml` (name → ip/mac/tier +
  `chassis`/`never_sleep`/`port`) is the source of truth. fleetd manages it via
  `register`/`deregister`; `fleetpower.py --sync` pulls it down to the local fallback; and
  `litellm-sync` regenerates the **gateway's** per-node routing entries from it
  (marker-fenced, so hand-managed `tier:s0`/`tier:s1` cloud entries stay untouched). A
  rebuilt node's new DHCP IP follows everywhere from one `register … overwrite` — the MAC
  (the WoL identity) is stable.
- **Chassis-aware ordering.** The per-chassis fan-controller node (`never_sleep`, same
  `chassis` id as its mates) is **first-to-wake** (mates gate until it's reachable — they're
  `blocked` if it never comes up) and **last-to-sleep** on a forced OFF, so boards never run
  without cooling. Nodes with no `chassis` (single/multi-GPU boxes) power in parallel.

The control-plane services (`dnc-litellm`, `dnc-fleetd`, `dnc-context`) run as systemd units
that auto-start on boot and self-heal on crash — see [`deploy/README.md`](deploy/README.md).

---

# Shared capabilities

Available in both modes.

## Admin UI, spend tracking & gateway auto-heal

The gateway routes fine without a database. The LiteLLM **web UI** (login, virtual keys,
spend/logs) needs Postgres — set `DATABASE_URL` and the launcher bootstraps Prisma
automatically on startup:

```bash
createdb litellm
export DATABASE_URL=postgresql://<user>:<pass>@127.0.0.1:5432/litellm
python -m dnc_router.serve --config litellm-config.yaml --host 0.0.0.0 --port 4000
#   → UI at http://<control-plane>:4000/ui  (login: admin + your LITELLM_MASTER_KEY)
```

**Auto-heal (LB-style):** `router_settings` sets health-check / cooldown / retry / fallback;
the custom strategy skips cooling-down and at-capacity deployments and rehashes their
traffic to a live sibling. `allowed_fails: 1` is deliberate — prefix affinity is
*deterministic*, so a retry re-picks the same deployment until it's cooled down. Sideways
failover needs **≥2 deployments per tier** (both shipped configs do this).

## Engineering personas

We adopt [`@chankov/agent-skills`](https://pi.dev/packages/@chankov/agent-skills)
(15 personas with per-persona model switching) rather than build our own, and extend it:

```bash
npx @chankov/agent-skills init        # installs the 15 stock personas into agents/
cp agents/*.md ~/.pi/agent/agents/    # make them spawnable as subagents
```

- **Added roles**: [Product Manager](agents/product-manager.md) and
  [Principal Engineer](agents/principal-engineer.md) — the two the package lacks.
- **Tier bindings**: [`.ai/agent-skills-overrides.md`](.ai/agent-skills-overrides.md) maps
  every persona to a squad tier, so persona subagents route through the custom router
  (complexity + cache affinity) just like interactive sessions.

Heavy design/review roles pin to the frontier (`s0`/`s1`), routine build/test roles to `s2`,
docs to `s3`, and several stay on `tier:auto` for per-turn complexity routing:

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

**Team fan-out**: drop an [`IDEA.md`](docs/ideas/README.md) written in a customer's voice,
and a PM + Architect turn it into requirements and a file plan, then engineer personas fan
out in parallel to build it — see [`docs/team-fanout-prompt.md`](docs/team-fanout-prompt.md)
and the [seed library](docs/ideas/README.md) (zero-to-one, tech-debt, bug-fix, port,
accessibility, performance).

## Long-term context

Shared semantic memory, partitioned by repo, in Postgres + pgvector. Durable facts are
embedded via `embed:qwen3` and retrieved to enrich vague prompts.

```bash
cd context && pip install -e .
cp .env.example .env               # set DNC_PG_DSN + embedding endpoint (gitignored)
python -m context.cli partition    # show the detected repo partition key
python -m context.cli recall "how does the router pick a squad?"
python -m context.cli serve        # HTTP sidecar on :7432 (POST /recall, /remember)
```

**Vague-prompt injection**: the Pi extension watches `before_agent_start`; when a prompt is
short/under-specified it calls the sidecar's `/recall` (passing the session cwd for
partition resolution), injects the top matches into the system prompt, and shows a
`pulled N context items` status. Override with `DNC_CONTEXT_URL`. If the sidecar is down,
the turn proceeds uninjected.

**Salience-judge writes**: at the end of each response (`agent_end`, debounced) and on
demand via `/remember`, the extension posts the new transcript to `/distill`. A cheap S3
judge (`DNC_JUDGE_MODEL`) extracts only durable facts — decisions, constraints, outcomes,
handoffs — which are embedded and written to the repo partition. Chatter stores nothing.
