"""Custom LiteLLM routing strategy: tier selection + prefix-hash KV-cache affinity.

Registered via `router_settings.routing_strategy: custom` (see litellm-config.yaml).

Two layers:
1. Tier selection — requests to virtual models (`tier:auto`, `tier:s0`..`tier:s3`)
   resolve to a squad based on caller hints (headers) or heuristics (prompt size).
2. Affinity — within a squad, consistent-hash the prompt prefix so requests that
   share a prefix (same repo context / system prompt) land on the same host and
   hit its KV/prompt cache. Spill to least-loaded host when the preferred host is
   saturated or unhealthy.
"""

from __future__ import annotations

import hashlib
from typing import Any

# Bytes of the serialized message-prefix used for affinity hashing. Coding-agent
# traffic shares long system prompts + repo context, so a large window is right;
# tune empirically (PLAN.md §6).
PREFIX_WINDOW_BYTES = 4096

# Above this estimated prompt token count, tier:auto escalates one squad.
CTX_ESCALATION_TOKENS = {"s3": 6_000, "s2": 24_000, "s1": 80_000}


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
    base = {"low": "s3", "medium": "s2", "high": "s1", "max": "s0"}[complexity]

    # Escalate if the context won't fit the tier's budget.
    tokens = estimate_tokens(messages)
    order = ["s3", "s2", "s1", "s0"]
    idx = order.index(base)
    while idx < len(order) - 1 and tokens > CTX_ESCALATION_TOKENS.get(order[idx], 10**9):
        idx += 1
    return order[idx]


class DncRoutingStrategy:
    """LiteLLM CustomRoutingStrategy: async get_available_deployment.

    healthy_deployments carry `model_info.dnc_squad` metadata (set by fleetd when
    it registers models). Selection: filter to squad → consistent-hash prefix →
    spill by load if the preferred deployment is busy.
    """

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

        # Consistent-hash affinity within the squad.
        h = int(prefix_hash(messages), 16)
        candidates.sort(key=lambda d: d.get("model_info", {}).get("id", ""))
        preferred = candidates[h % len(candidates)]

        # TODO(M1): spill-over — check in-flight request count / queue depth from
        # router metrics; if preferred is saturated, fall back to least-busy candidate.
        return preferred
