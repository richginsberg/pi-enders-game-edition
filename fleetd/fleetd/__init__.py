"""fleetd — sidecar daemon for the divide-and-conquer fleet.

Responsibilities (see PLAN.md §3):
- Host inventory & catalog (SQLite)
- SSH/Docker IaC plays: install, upgrade, deploy, configure
- Health/metrics polling and LiteLLM model registration
- Task ledger for the relentless harness (DoD state, tmux sessions)
"""

__version__ = "0.1.0"
