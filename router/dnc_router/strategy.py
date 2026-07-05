"""Custom LiteLLM routing strategy: tier selection + prefix-hash KV-cache affinity.

Attached to a live Router via `Router.set_custom_routing_strategy()` from the
programmatic launcher (`dnc_router.serve`) — the proxy YAML has no key for this.
`set_custom_routing_strategy` rebinds the router's (async_)get_available_deployment
to this instance's bound methods, so the router calls us with `self`=strategy and
no deployment list; we hold a router reference and read candidates from it.

Kept import-free of litellm (the setter duck-types, no isinstance check) so the pure
routing logic stays unit-testable without the proxy installed.

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

# Per-squad soft concurrency: spill to another member once the affinity-preferred
# deployment has this many selections in LOAD_WINDOW_S. S3 nodes (BC-250) serve
# single-slot (`--parallel 1`), so a second concurrent request just queues behind a
# multi-minute prefill — treat one recent request as "busy" and spread instead of
# dogpiling. Squads not listed use SPILL_THRESHOLD.
SQUAD_CONCURRENCY = {"s3": 1}


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
    unhealthy_ids: frozenset[str] | set[str] = frozenset(),
    spill_threshold: int = SPILL_THRESHOLD,
) -> dict:
    """Prefix-hash affinity with load-based spill-over, skipping unhealthy hosts.

    `candidates` is non-empty. Deployments in `unhealthy_ids` (cooling down / failing a
    health check) are removed *before* the affinity hash, so a downed host deterministically
    rehashes its traffic to a live sibling instead of black-holing it. If every candidate is
    unhealthy we fall back to the full set (better to try a maybe-stale host than 500 — the
    request-level retry/fallback is the backstop).
    """
    healthy = [c for c in candidates if deployment_id(c) not in unhealthy_ids] or candidates
    ordered = sorted(healthy, key=deployment_id)
    h = int(prefix_hash(messages), 16)
    preferred = ordered[h % len(ordered)]

    chosen = preferred
    if len(ordered) > 1 and tracker.load(deployment_id(preferred), now) >= spill_threshold:
        chosen = min(ordered, key=lambda d: tracker.load(deployment_id(d), now))

    tracker.record(deployment_id(chosen), now)
    return chosen


def _as_dict(deployment: Any) -> dict:
    """Normalize a router deployment (dict or pydantic Deployment) to a dict."""
    if isinstance(deployment, dict):
        return deployment
    if hasattr(deployment, "model_dump"):
        return deployment.model_dump()
    return dict(deployment)  # last resort


def _headers(request_kwargs: dict | None) -> dict[str, str]:
    """Pull the incoming request headers wherever the proxy stashed them."""
    rk = request_kwargs or {}
    psr = rk.get("proxy_server_request") or {}
    return psr.get("headers") or rk.get("metadata", {}).get("headers") or {}


class DncRoutingStrategy:
    """Router-attached strategy. `set_custom_routing_strategy` rebinds the router's
    (async_)get_available_deployment to these bound methods.

    Deployments carry `model_info.dnc_squad` metadata. Selection: resolve the model
    group's members → pick squad (tier/complexity) → consistent-hash prefix → spill
    to least-loaded member when the preferred deployment is saturated.

    Replacing the router's method bypasses its built-in cooldown/health filter, so we
    re-apply it: `_unhealthy()` reads the router's cooldown state and `select_deployment`
    excludes those hosts. This is the "route around a downed node" behavior — a host that
    fails / is cooling down drops out of affinity and spill until it recovers. The
    request-level retry+fallback (litellm-config) is the backstop when our snapshot is stale.
    """

    def __init__(self, router: Any = None) -> None:
        self.router = router
        self.tracker = LoadTracker()

    def _unhealthy(self) -> set[str]:
        """Best-effort set of deployment ids the router considers unusable (cooling down
        after failures). Tolerant of litellm version differences — any problem degrades to
        an empty set (no pre-filtering), never breaks routing. The pure filtering logic is
        tested via select_deployment; this is thin, defensive glue over litellm internals."""
        r = self.router
        if r is None:
            return set()
        try:
            getter = getattr(r, "_get_cooldown_deployments", None)
            res = getter() if callable(getter) else None
            if isinstance(res, (list, set, tuple)):
                return {str(x) for x in res}
            # some versions return (cooldown_list, cooldown_times)
            if isinstance(res, tuple) and res and isinstance(res[0], (list, set)):
                return {str(x) for x in res[0]}
        except Exception:
            pass
        return set()

    def _members(self, model: str) -> list[dict]:
        """All configured deployments in the requested model group (e.g. 'tier:auto')."""
        r = self.router
        raw: list[Any] = []
        if r is not None:
            get_list = getattr(r, "get_model_list", None)
            if callable(get_list):
                raw = get_list(model_name=model) or []
            if not raw:
                raw = [d for d in getattr(r, "model_list", []) if _as_dict(d).get("model_name") == model]
        return [_as_dict(d) for d in raw]

    def _choose(self, model: str, messages: list[dict[str, Any]] | None, request_kwargs: dict | None) -> dict | None:
        messages = messages or []
        squad = pick_tier(model, _headers(request_kwargs), messages)
        members = self._members(model)
        if not members:
            return None
        candidates = [d for d in members if d.get("model_info", {}).get("dnc_squad") == squad] or members
        return select_deployment(
            candidates,
            messages,
            self.tracker,
            unhealthy_ids=self._unhealthy(),
            spill_threshold=SQUAD_CONCURRENCY.get(squad, SPILL_THRESHOLD),
        )

    async def async_get_available_deployment(
        self,
        model: str,
        messages: list[dict[str, Any]] | None = None,
        input: list | str | None = None,
        specific_deployment: bool | None = False,
        request_kwargs: dict | None = None,
    ) -> dict | None:
        return self._choose(model, messages, request_kwargs)

    def get_available_deployment(
        self,
        model: str,
        messages: list[dict[str, Any]] | None = None,
        input: list | str | None = None,
        specific_deployment: bool | None = False,
        request_kwargs: dict | None = None,
    ) -> dict | None:
        return self._choose(model, messages, request_kwargs)
