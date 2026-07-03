"""Custom LiteLLM routing strategy: tier selection + prefix-hash KV-cache affinity.

Registered via `router_settings.routing_strategy: custom` (see litellm-config.yaml).

Two layers:
1. Tier selection — requests to virtual models (`tier:auto`, `tier:s0`..`tier:s3`)
   resolve to a squad based on caller hints (headers) or heuristics (prompt size).
2. Affinity — within a squad, consistent-hash the prompt prefix so requests that
   share a prefix (same repo context / system prompt) land on the same host and
   hit its KV/prompt cache. Spill to the least-loaded host when the preferred
   host is saturated.
"""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict, deque
from typing import Any

# Bytes of the serialized message-prefix used for affinity hashing. Coding-agent
# traffic shares long system prompts + repo context, so a large window is right;
# tune empirically (PLAN.md §6).
PREFIX_WINDOW_BYTES = 4096

# Above this estimated prompt token count, tier:auto escalates one squad.
CTX_ESCALATION_TOKENS = {"s3": 6_000, "s2": 24_000, "s1": 80_000}

# Spill-over: if the affinity-preferred deployment has seen more than this many
# selections inside LOAD_WINDOW_S, route to the least-loaded squad member
# instead. Approximates in-flight depth without hooking request completion —
# coding-agent requests typically run tens of seconds, similar to the window.
LOAD_WINDOW_S = 30.0
SPILL_THRESHOLD = 4


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    chars = sum(len(str(m.get("content", ""))) for m in messages)
    return chars // 4


def prefix_hash(messages: list[dict[str, Any]]) -> str:
    blob = "".join(str(m.get("content", "")) for m in messages).encode()[:PREFIX_WINDOW_BYTES]
    return hashlib.sha256(blob).hexdigest()


def pick_tier(requested: str, headers: dict[str, str], messages: list[dict[str, Any]]) -> str:
    """Resolve `tier:auto` to a concrete squad. Explicit tiers pass through."""
    if requested != "tier:auto":
        return requested.removeprefix("tier:")

    hinted = headers.get("x-dnc-tier")
    if hinted:
        return hinted

    complexity = headers.get("x-dnc-complexity", "medium")  # low|medium|high|max
    base = {"low": "s3", "medium": "s2", "high": "s1", "max": "s0"}.get(complexity, "s2")

    # Escalate if the context won't fit the tier's budget.
    tokens = estimate_tokens(messages)
    order = ["s3", "s2", "s1", "s0"]
    idx = order.index(base)
    while idx < len(order) - 1 and tokens > CTX_ESCALATION_TOKENS.get(order[idx], 10**9):
        idx += 1
    return order[idx]


class LoadTracker:
    """Rolling count of recent selections per deployment id (load proxy)."""

    def __init__(self, window_s: float = LOAD_WINDOW_S) -> None:
        self.window_s = window_s
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def record(self, deployment_id: str, now: float | None = None) -> None:
        self._events[deployment_id].append(now if now is not None else time.monotonic())

    def load(self, deployment_id: str, now: float | None = None) -> int:
        now = now if now is not None else time.monotonic()
        q = self._events[deployment_id]
        while q and q[0] < now - self.window_s:
            q.popleft()
        return len(q)


def deployment_id(d: dict) -> str:
    info = d.get("model_info", {})
    return str(info.get("id") or d.get("litellm_params", {}).get("api_base") or id(d))


def select_deployment(
    candidates: list[dict],
    messages: list[dict[str, Any]],
    tracker: LoadTracker,
    now: float | None = None,
) -> dict:
    """Prefix-hash affinity with load-based spill-over. `candidates` is non-empty."""
    ordered = sorted(candidates, key=deployment_id)
    h = int(prefix_hash(messages), 16)
    preferred = ordered[h % len(ordered)]

    chosen = preferred
    if len(ordered) > 1 and tracker.load(deployment_id(preferred), now) >= SPILL_THRESHOLD:
        chosen = min(ordered, key=lambda d: tracker.load(deployment_id(d), now))

    tracker.record(deployment_id(chosen), now)
    return chosen


class DncRoutingStrategy:
    """LiteLLM CustomRoutingStrategy: async get_available_deployment.

    healthy_deployments carry `model_info.dnc_squad` metadata (set by fleetd when
    it registers models). Selection: filter to squad → consistent-hash prefix →
    spill to least-loaded member when the preferred deployment is saturated.
    """

    def __init__(self) -> None:
        self.tracker = LoadTracker()

    async def async_get_available_deployment(
        self,
        model: str,
        healthy_deployments: list[dict],
        messages: list[dict[str, Any]] | None = None,
        request_kwargs: dict | None = None,
    ) -> dict | None:
        messages = messages or []
        headers = (request_kwargs or {}).get("proxy_server_request", {}).get("headers", {}) or {}

        squad = pick_tier(model, headers, messages)
        candidates = [
            d for d in healthy_deployments
            if d.get("model_info", {}).get("dnc_squad") == squad
        ] or healthy_deployments
        if not candidates:
            return None

        return select_deployment(candidates, messages, self.tracker)
