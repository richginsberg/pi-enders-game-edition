# Divide and Conquer

Fleet-scale AI coding harness built on [Pi](https://pi.dev): orchestrates squads of
heterogeneous GPU inference hosts (Pascal → Volta → Ampere → frontier APIs) with
complexity-tiered, KV-cache-affinity routing, relentless task completion, and
TUI-driven fleet governance. See [PLAN.md](PLAN.md) for the full design.

## Layout

| Directory | Language | Purpose |
|---|---|---|
| [`fleetd/`](fleetd/) | Python | Sidecar daemon: host inventory, SSH/Docker IaC plays, health polling, task ledger, LiteLLM registration |
| [`router/`](router/) | Python | LiteLLM proxy config + custom routing strategy (tier selection, prefix-hash cache affinity) |
| [`pi-ext/`](pi-ext/) | TypeScript | Pi extension: fleet provider (models from LiteLLM), `/fleet`, `/deploy`, `/tasks` commands |
| [`harness/`](harness/) | TypeScript | Relentless (Ralph-loop) runner: DoD ledger, judge/enlistment, subagent fan-out |
| [`context/`](context/) | Python | Long-term context: repo-partitioned pgvector store + `embed:qwen3` client; `remember()`/`recall()` for the RAG layer |
| [`agents/`](agents/) | Markdown | Engineering personas (Product Manager, Principal Engineer) added on top of `@chankov/agent-skills`, bound to fleet tiers |
| [`tools/`](tools/) | Python | Operational scripts (e.g. `bench_embed.py` — embedding-endpoint latency benchmark) |
| `pi/` | — | Upstream Pi clone, reference only (not part of this repo) |

## Quick start (M1)

```bash
# 1. Control-plane box: LiteLLM with the custom router
cd router && pip install -e .
cp litellm-config.example.yaml litellm-config.yaml  # local copy, gitignored — add your endpoints/keys
litellm --config litellm-config.yaml

# 2. fleetd
cd fleetd && pip install -e . && fleetd serve

# 3. Pi extension (symlink into Pi's extension dir)
cd pi-ext && npm install
ln -s "$(pwd)" ~/.pi/agent/extensions/divide-and-conquer
```

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

## Long-term context (M5)

Shared semantic memory, partitioned by repo, in Postgres + pgvector. Durable facts
are embedded via the fleet's `embed:qwen3` and retrieved to enrich vague prompts.

```bash
cd context && pip install -e .
cp .env.example .env               # set DNC_PG_DSN + embedding endpoint (gitignored)
python -m context.cli partition    # show the detected repo partition key
python -m context.cli recall "how does the router pick a squad?"
```

`ContextService.remember(items)` / `recall(query)` are the API the salience-judge
write path and vague-prompt injection build on. Requires pgvector ≥ 0.5 (HNSW index).
