"""When to rent, how many, and when to give it back.

A rented GPU bills wall-clock, so the only way it beats a hosted API is **saturation**: you
must keep it busy for hours. This module encodes that judgement as pure functions so the
decision is auditable and testable without spending a cent.

The economics, once:

    effective $/M out = hourly_rate / (sustained_tok_s * 3600 / 1e6)

Below the break-even throughput the fallback API is cheaper, and every idle second makes it
worse. Hence the gate: burst only for **millions of output tokens**, over **hours**, at
**high saturation**, with a **margin** over what the fallback tier would have cost.

Guardrails are not optional here — the failure mode is silently spending money. Every burst
node carries a hard lifetime, an idle TTL, and a daily spend cap; unused *volumes* are reaped
on their own clock because they keep billing long after the pod is gone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Verdict(str, Enum):
    BURST = "burst"
    USE_FALLBACK = "use_fallback"
    NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True)
class Economics:
    """Cost model for one burst node vs the tier it would displace."""
    gpu_usd_per_hr: float
    expected_tok_s: float           # aggregate sustained output across all sequences
    fallback_usd_per_m_out: float   # what the higher tier charges per 1M output tokens

    def effective_usd_per_m(self) -> float:
        """$ per 1M output tokens if we actually sustain expected_tok_s."""
        if self.expected_tok_s <= 0:
            return float("inf")
        return self.gpu_usd_per_hr / (self.expected_tok_s * 3600 / 1e6)

    def breakeven_tok_s(self) -> float:
        """Sustained rate at which renting exactly matches the fallback API."""
        if self.fallback_usd_per_m_out <= 0:
            return float("inf")
        return self.gpu_usd_per_hr * 1e6 / (self.fallback_usd_per_m_out * 3600)

    def breakeven_tokens_per_hour(self) -> float:
        return self.breakeven_tok_s() * 3600

    def beats_fallback(self) -> bool:
        return self.effective_usd_per_m() < self.fallback_usd_per_m_out


@dataclass(frozen=True)
class Demand:
    """What we expect the tier to be asked for."""
    projected_output_tokens: int
    projected_hours: float
    #: Fraction of expected_tok_s we believe we can actually hold (fan-out saturates; a
    #: single interactive stream does not).
    expected_saturation: float = 1.0


@dataclass(frozen=True)
class Policy:
    """Operator-tunable thresholds. Defaults are deliberately conservative."""
    enabled: bool = False              # burst is opt-in; default off
    min_output_tokens: int = 5_000_000  # "tokens in the millions"
    min_duration_hours: float = 1.0
    min_saturation: float = 0.70
    savings_margin: float = 1.5        # must be >=1.5x cheaper than the fallback
    idle_ttl_s: int = 900              # 15 min with no requests -> scale to zero
    max_lifetime_s: int = 6 * 3600     # hard deadline regardless of activity
    max_replicas: int = 4
    daily_usd_cap: float = 50.0
    volume_max_idle_days: int = 14     # destroy the weights volume if unused this long
    per_node_tok_s: float = 0.0        # measured aggregate throughput of one node


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str
    replicas: int = 0
    projected_cost_usd: float = 0.0
    fallback_cost_usd: float = 0.0

    @property
    def savings_usd(self) -> float:
        return self.fallback_cost_usd - self.projected_cost_usd

    @property
    def ok(self) -> bool:
        return self.verdict is Verdict.BURST


def should_burst(
    demand: Demand, econ: Economics, policy: Policy, *, spent_today_usd: float = 0.0
) -> Decision:
    """The gate. Returns BURST only when renting is clearly, quantifiably right."""
    fallback_cost = demand.projected_output_tokens / 1e6 * econ.fallback_usd_per_m_out

    def no(reason: str) -> Decision:
        return Decision(Verdict.USE_FALLBACK, reason, 0, 0.0, fallback_cost)

    if not policy.enabled:
        return Decision(Verdict.NOT_CONFIGURED, "burst tier is not enabled", 0, 0.0, fallback_cost)

    if demand.projected_output_tokens < policy.min_output_tokens:
        return no(f"projected {demand.projected_output_tokens:,} output tokens is under the "
                  f"{policy.min_output_tokens:,} minimum — a rented GPU bills wall-clock, so "
                  f"small jobs are cheaper on the API")

    if demand.projected_hours < policy.min_duration_hours:
        return no(f"projected {demand.projected_hours:.2f}h is under the "
                  f"{policy.min_duration_hours}h minimum — startup and idle would dominate")

    if demand.expected_saturation < policy.min_saturation:
        return no(f"expected saturation {demand.expected_saturation:.0%} is under "
                  f"{policy.min_saturation:.0%} — an under-fed GPU bills the same as a busy one")

    if not econ.beats_fallback():
        return no(f"effective ${econ.effective_usd_per_m():.3f}/M does not beat the fallback "
                  f"${econ.fallback_usd_per_m_out:.3f}/M at {econ.expected_tok_s:.0f} tok/s "
                  f"(need >{econ.breakeven_tok_s():.0f} tok/s)")

    # Would it actually generate enough tokens per rented hour to clear break-even * margin?
    required = econ.breakeven_tokens_per_hour() * demand.projected_hours * policy.savings_margin
    if demand.projected_output_tokens < required:
        return no(f"projected {demand.projected_output_tokens:,.0f} tokens is under the "
                  f"{required:,.0f} needed to beat the fallback by {policy.savings_margin}x "
                  f"over {demand.projected_hours:.1f}h")

    replicas = plan_replicas(demand, policy)
    projected_cost = econ.gpu_usd_per_hr * demand.projected_hours * replicas

    if spent_today_usd + projected_cost > policy.daily_usd_cap:
        return no(f"would exceed the daily cap: ${spent_today_usd:.2f} spent + "
                  f"${projected_cost:.2f} projected > ${policy.daily_usd_cap:.2f}")

    return Decision(
        Verdict.BURST,
        f"{replicas} node(s) x {demand.projected_hours:.1f}h at ${econ.gpu_usd_per_hr:.2f}/hr "
        f"= ${projected_cost:.2f}, vs ${fallback_cost:.2f} on the fallback tier "
        f"(saves ${fallback_cost - projected_cost:.2f})",
        replicas, projected_cost, fallback_cost,
    )


def plan_replicas(demand: Demand, policy: Policy) -> int:
    """How many nodes to run. Scale by the rate the work implies, not by token total:
    N nodes only help if the work arrives concurrently."""
    if policy.per_node_tok_s <= 0 or demand.projected_hours <= 0:
        return 1
    required_tok_s = demand.projected_output_tokens / (demand.projected_hours * 3600)
    n = int(-(-required_tok_s // policy.per_node_tok_s))  # ceil
    return max(1, min(n, policy.max_replicas))


# --- lifecycle decisions -------------------------------------------------------------
@dataclass(frozen=True)
class NodeState:
    """What the reaper needs to know about a live burst node."""
    node_id: str
    age_s: float
    idle_s: float                  # seconds since the last request completed
    inflight: int = 0
    usd_per_hr: float = 0.0

    @property
    def accrued_usd(self) -> float:
        return self.usd_per_hr * self.age_s / 3600


class Action(str, Enum):
    KEEP = "keep"
    SCALE_TO_ZERO = "scale_to_zero"
    KILL_EXPIRED = "kill_expired"


def lifecycle_action(node: NodeState, policy: Policy) -> tuple[Action, str]:
    """Per-node keep/kill. A hard lifetime beats an idle timer: an idle timer can be reset
    forever by a trickle of traffic, and a wedged node may never report idle at all."""
    if node.age_s >= policy.max_lifetime_s:
        return Action.KILL_EXPIRED, (
            f"hit max lifetime {policy.max_lifetime_s}s (accrued ${node.accrued_usd:.2f}) — "
            f"hard deadline, {node.inflight} request(s) will be drained")
    if node.inflight > 0:
        return Action.KEEP, f"{node.inflight} request(s) in flight"
    if node.idle_s >= policy.idle_ttl_s:
        return Action.SCALE_TO_ZERO, (
            f"idle {node.idle_s:.0f}s >= TTL {policy.idle_ttl_s}s "
            f"(accrued ${node.accrued_usd:.2f})")
    return Action.KEEP, f"idle {node.idle_s:.0f}s < TTL {policy.idle_ttl_s}s"


@dataclass(frozen=True)
class VolumeState:
    volume_id: str
    size_gb: float
    days_since_last_use: float
    usd_per_gb_month: float = 0.07

    @property
    def monthly_usd(self) -> float:
        return self.size_gb * self.usd_per_gb_month


def volume_action(vol: VolumeState, policy: Policy) -> tuple[bool, str]:
    """Weights volumes outlive pods by design (that's the point — they kill cold starts),
    which means they also bill forever if nobody notices. Reap the ones nobody has booted
    against in `volume_max_idle_days`. Returns (should_destroy, reason)."""
    if vol.days_since_last_use >= policy.volume_max_idle_days:
        return True, (f"volume {vol.volume_id} unused {vol.days_since_last_use:.1f}d >= "
                      f"{policy.volume_max_idle_days}d, costing ${vol.monthly_usd:.2f}/mo")
    return False, (f"volume {vol.volume_id} last used {vol.days_since_last_use:.1f}d ago "
                   f"(reap at {policy.volume_max_idle_days}d)")


# --- cold-start spillover -------------------------------------------------------------
#: A burst node takes minutes to become useful. During that window the tier must NOT be
#: routable, or the gateway will send work into a loading server. The mechanism is simply:
#: never register the deployment until it answers /v1/models. With no healthy member, the
#: gateway's existing tier fallback carries the work UP to the higher (subscription/API)
#: tier automatically — no extra routing code is needed.
COLD_START_CONTRACT = (
    "Register a burst deployment ONLY after a real /v1/models probe succeeds. Until then the "
    "tier has no healthy members and LiteLLM's tier fallback routes the work up to the "
    "higher tier. Deregister BEFORE terminating, and drain in-flight requests."
)


def spillover_tier(tier: str, order: tuple[str, ...] = ("s3", "s2", "s1", "s0")) -> str | None:
    """Tier that absorbs this tier's work while a burst node is still coming up.

    `order` runs cheapest -> most capable, so "spill up" is the next index. s0 has nowhere
    to go: if the frontier tier is down, the request fails rather than silently downgrading.
    """
    if tier not in order:
        return None
    i = order.index(tier) + 1
    return order[i] if i < len(order) else None
