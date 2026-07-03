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
| `pi/` | — | Upstream Pi clone, reference only (not part of this repo) |

## Quick start (M1)

```bash
# 1. Control-plane box: LiteLLM with the custom router
cd router && pip install -e . && litellm --config litellm-config.yaml

# 2. fleetd
cd fleetd && pip install -e . && fleetd serve

# 3. Pi extension (symlink into Pi's extension dir)
cd pi-ext && npm install
ln -s "$(pwd)" ~/.pi/agent/extensions/divide-and-conquer
```
