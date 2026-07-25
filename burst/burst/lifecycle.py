"""Orchestrator: rent -> serve -> register -> idle -> give back.

Two invariants carry most of the safety:

1. **Never register a deployment until a real ``/v1/models`` probe succeeds.** The provider
   saying "running" only means a container exists. Until we register, the tier has no healthy
   member and the gateway's tier fallback carries the work up to the higher tier — which is
   exactly the cold-start spillover behaviour we want, with no extra routing code.
2. **Deregister before terminating, and drain.** The reverse order strands in-flight requests.

Everything with a clock or a socket is injectable so the whole flow is testable offline.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from .drivers.base import BurstDriver, BurstNode, Phase
from .policy import Action, Economics, NodeState, Policy, lifecycle_action
from .profiles import GPUS
from .template import render

PROVISION_BUDGET_S = 900.0   # weights pull can take minutes on a cold volume
POLL_S = 10.0


@dataclass
class BurstManager:
    """Owns the live burst nodes and their money."""
    driver: BurstDriver
    policy: Policy
    #: called with (node) once it is serving, to add it to the gateway
    register_cb: Callable[[BurstNode], Any] | None = None
    #: called with (node) before termination, to remove it from the gateway
    deregister_cb: Callable[[BurstNode], Any] | None = None
    now: Callable[[], float] = time.time
    nodes: dict[str, BurstNode] = field(default_factory=dict)
    spent_today_usd: float = 0.0

    # -- bring up ------------------------------------------------------------------
    async def scale_up(
        self, *, model: str, quant: str, gpu_key: str | None = None,
        context: int = 262144, replicas: int = 1, volume_name: str = "dnc-burst-weights",
        volume_gb: int = 100, probe: Callable[[str], Any] | None = None,
    ) -> AsyncIterator[dict]:
        """Rent `replicas` nodes and stream progress. Yields event dicts shaped like the
        fleet-power SSE stream so the same UI renders both."""
        probe = probe or _probe_models
        offers = self.driver.search(min_vram_gb=1, gpu_key=gpu_key)
        if not offers:
            yield {"type": "error", "detail": "no offers with stock"}
            return

        volume_id = None
        try:
            volume_id = self.driver.ensure_volume(volume_name, volume_gb)
            yield {"type": "volume", "volume_id": volume_id, "size_gb": volume_gb}
        except Exception as exc:  # noqa: BLE001 — a volume is an optimisation, not a hard dep
            yield {"type": "warn", "detail": f"no network volume ({exc}); "
                                             "cold start will pull weights from HuggingFace"}

        for i in range(replicas):
            offer = offers[0] if gpu_key else _pick(offers, model, quant, context)
            if offer is None:
                yield {"type": "error", "detail": "no offer can serve this model/quant/context"}
                return
            tpl = render(model, quant, offer.gpu_key, context=context)
            for w in tpl.warnings:
                yield {"type": "warn", "detail": w}

            node = self.driver.create(offer, image=tpl.image, args=tpl.args, env=tpl.env,
                                      volume_id=volume_id)
            node.model, node.quant = model, quant
            node.created_at = self.now()
            self.nodes[node.node_id] = node
            yield {"type": "node", "node_id": node.node_id, "phase": Phase.CREATING,
                   "gpu": offer.gpu_key, "usd_per_hr": node.usd_per_hr,
                   "max_num_seqs": tpl.plan.max_num_seqs, "replica": i + 1}

            async for ev in self._await_serving(node, probe):
                yield ev

    async def _await_serving(self, node: BurstNode, probe) -> AsyncIterator[dict]:
        """Poll to SERVING (or give up). Registers only on a real model-list response."""
        start = self.now()
        last: Phase | None = None
        while True:
            elapsed = self.now() - start
            phase = self.driver.status(node)

            if phase is Phase.LOADING and await probe(node.endpoint):
                phase = Phase.SERVING

            if phase is Phase.ZERO_GPU:
                # Documented RunPod outcome: reachable, billing, no GPU. Never usable.
                yield {"type": "node", "node_id": node.node_id, "phase": Phase.ZERO_GPU,
                       "detail": "allocated with zero GPUs — terminating and retrying elsewhere"}
                self.driver.terminate(node)
                node.phase = Phase.GONE
                self.nodes.pop(node.node_id, None)
                return

            if phase != last:
                yield {"type": "node", "node_id": node.node_id, "phase": phase,
                       "elapsed_s": round(elapsed, 1)}
                last = phase
            node.phase = phase

            if phase is Phase.SERVING:
                node.last_request_at = self.now()
                if self.register_cb:
                    await _maybe_await(self.register_cb(node))
                node.registered = True
                yield {"type": "registered", "node_id": node.node_id,
                       "endpoint": node.endpoint, "elapsed_s": round(elapsed, 1)}
                return
            if phase in (Phase.GONE, Phase.ERROR):
                self.nodes.pop(node.node_id, None)
                return
            if elapsed >= PROVISION_BUDGET_S:
                yield {"type": "node", "node_id": node.node_id, "phase": Phase.ERROR,
                       "detail": f"never served within {PROVISION_BUDGET_S:.0f}s — terminating"}
                self.driver.terminate(node)
                self.nodes.pop(node.node_id, None)
                return
            await asyncio.sleep(POLL_S)

    # -- tear down -----------------------------------------------------------------
    async def release(self, node: BurstNode, reason: str = "") -> dict:
        """Deregister, drain, terminate. Safe to call twice."""
        if node.registered and self.deregister_cb:
            await _maybe_await(self.deregister_cb(node))
            node.registered = False
        try:
            self.driver.terminate(node)
        finally:
            self.nodes.pop(node.node_id, None)
            self.spent_today_usd += node.usd_per_hr * max(0.0, self.now() - node.created_at) / 3600
        return {"type": "released", "node_id": node.node_id, "reason": reason,
                "accrued_usd": round(node.usd_per_hr * (self.now() - node.created_at) / 3600, 4)}

    async def reap(self) -> list[dict]:
        """One pass of the idle/TTL reaper. Call on a timer — this is the feature that keeps
        a forgotten pod from costing ~$41/day."""
        out = []
        for node in list(self.nodes.values()):
            state = NodeState(node.node_id, self.now() - node.created_at,
                              self.now() - (node.last_request_at or node.created_at),
                              int(node.meta.get("inflight", 0)), node.usd_per_hr)
            action, reason = lifecycle_action(state, self.policy)
            if action is not Action.KEEP:
                out.append(await self.release(node, f"{action.value}: {reason}"))
        return out

    def reconcile(self) -> list[BurstNode]:
        """Find machines we own that the manager forgot — a crash mid-provision leaks a pod
        that bills until someone notices. Run at startup, and periodically."""
        return [n for n in self.driver.list_nodes() if n.node_id not in self.nodes]

    # -- reporting -----------------------------------------------------------------
    def status(self) -> dict:
        nodes = []
        for n in self.nodes.values():
            age = self.now() - n.created_at
            nodes.append({
                "node_id": n.node_id, "gpu": n.gpu_key, "model": n.model, "quant": n.quant,
                "phase": n.phase, "registered": n.registered, "endpoint": n.endpoint,
                "elapsed_s": round(age, 1), "usd_per_hr": n.usd_per_hr,
                "accrued_usd": round(n.usd_per_hr * age / 3600, 4),
                "idle_s": round(self.now() - (n.last_request_at or n.created_at), 1),
            })
        return {
            "nodes": nodes,
            "count": len(nodes),
            "burn_usd_per_hr": round(sum(n.usd_per_hr for n in self.nodes.values()), 4),
            "spent_today_usd": round(self.spent_today_usd, 4),
            "daily_cap_usd": self.policy.daily_usd_cap,
        }

    def economics(self, gpu_key: str, expected_tok_s: float, fallback_usd_per_m: float) -> Economics:
        return Economics(GPUS[gpu_key].usd_per_hr, expected_tok_s, fallback_usd_per_m)


def _pick(offers, model: str, quant: str, context: int):
    """First offer whose card can actually serve this model/quant/context."""
    from .profiles import plan_capacity
    for o in offers:
        plan = plan_capacity(o.gpu_key, model, quant, context=context)
        if plan.fits and plan.max_num_seqs >= 1:
            return o
    return None


async def _probe_models(endpoint: str | None) -> bool:
    """The only signal allowed to promote a node to SERVING."""
    if not endpoint:
        return False
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            return (await c.get(f"{endpoint}/models")).status_code == 200
    except Exception:  # noqa: BLE001
        return False


async def _maybe_await(v):
    return await v if asyncio.iscoroutine(v) else v


def new_node_id() -> str:
    return f"burst-{uuid.uuid4().hex[:8]}"
