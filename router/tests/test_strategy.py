from dnc_router.strategy import (
    SPILL_THRESHOLD,
    DncRoutingStrategy,
    LoadTracker,
    pick_tier,
    select_deployment,
)


def dep(dep_id: str, squad: str, model_name: str = "tier:auto") -> dict:
    return {"model_name": model_name, "model_info": {"id": dep_id, "dnc_squad": squad}}


def msgs(text: str) -> list[dict]:
    return [{"role": "user", "content": text}]


# --- pick_tier ---------------------------------------------------------------

def test_explicit_tier_passthrough():
    assert pick_tier("tier:s1", {}, []) == "s1"


def test_auto_complexity_mapping():
    assert pick_tier("tier:auto", {"x-dnc-complexity": "low"}, msgs("hi")) == "s3"
    assert pick_tier("tier:auto", {"x-dnc-complexity": "max"}, msgs("hi")) == "s0"
    assert pick_tier("tier:auto", {}, msgs("hi")) == "s2"  # default medium


def test_auto_header_hint_wins():
    assert pick_tier("tier:auto", {"x-dnc-tier": "s1"}, msgs("hi")) == "s1"


def test_context_escalation():
    # ~40k estimated tokens exceeds s3 (6k) and s2 (24k) budgets -> s1
    big = msgs("x" * 160_000)
    assert pick_tier("tier:auto", {"x-dnc-complexity": "low"}, big) == "s1"


# --- affinity ------------------------------------------------------------------

def test_affinity_is_stable():
    candidates = [dep("a", "s2"), dep("b", "s2"), dep("c", "s2")]
    first = select_deployment(candidates, msgs("shared prefix"), LoadTracker())
    for _ in range(5):
        again = select_deployment(candidates, msgs("shared prefix"), LoadTracker())
        assert again["model_info"]["id"] == first["model_info"]["id"]


def test_affinity_independent_of_candidate_order():
    a, b, c = dep("a", "s2"), dep("b", "s2"), dep("c", "s2")
    r1 = select_deployment([a, b, c], msgs("p"), LoadTracker())
    r2 = select_deployment([c, a, b], msgs("p"), LoadTracker())
    assert r1["model_info"]["id"] == r2["model_info"]["id"]


# --- spill-over ------------------------------------------------------------------

def test_spill_over_when_preferred_saturated():
    candidates = [dep("a", "s3"), dep("b", "s3")]
    tracker = LoadTracker()
    m = msgs("hot prefix")
    seen = [
        select_deployment(candidates, m, tracker, now=1.0)["model_info"]["id"]
        for _ in range(SPILL_THRESHOLD + 2)
    ]
    # First SPILL_THRESHOLD go to the preferred host, then spill to the other.
    assert len(set(seen[:SPILL_THRESHOLD])) == 1
    assert set(seen) == {"a", "b"}


def test_load_window_expiry():
    tracker = LoadTracker(window_s=10)
    tracker.record("a", now=0.0)
    tracker.record("a", now=1.0)
    assert tracker.load("a", now=5.0) == 2
    assert tracker.load("a", now=20.0) == 0


def test_single_candidate_never_spills():
    candidates = [dep("only", "s1")]
    tracker = LoadTracker()
    for _ in range(20):
        chosen = select_deployment(candidates, msgs("p"), tracker, now=1.0)
        assert chosen["model_info"]["id"] == "only"


# --- strategy end-to-end ------------------------------------------------------------

class FakeRouter:
    """Stand-in for litellm.Router: the strategy pulls group members from here."""

    def __init__(self, deployments: list[dict], with_get_model_list: bool = True) -> None:
        self.model_list = deployments
        if not with_get_model_list:
            self.get_model_list = None  # force the model_list fallback path

    def get_model_list(self, model_name=None):  # noqa: D401 - mimic litellm signature
        return [d for d in self.model_list if d.get("model_name") == model_name]


def _run(strat, model, headers=None, text="hello"):
    import asyncio

    rk = {"proxy_server_request": {"headers": headers or {}}}
    return asyncio.run(strat.async_get_available_deployment(model, messages=msgs(text), request_kwargs=rk))


def test_strategy_auto_routes_by_complexity_header():
    deployments = [dep("s0-a", "s0"), dep("s1-a", "s1"), dep("s3-a", "s3"), dep("s3-b", "s3")]
    strat = DncRoutingStrategy(router=FakeRouter(deployments))
    assert _run(strat, "tier:auto", {"x-dnc-complexity": "low"})["model_info"]["dnc_squad"] == "s3"
    assert _run(strat, "tier:auto", {"x-dnc-complexity": "max"})["model_info"]["dnc_squad"] == "s0"


def test_strategy_explicit_tier_group():
    # explicit tier groups: members all share the model_name, one squad
    deployments = [dep("s1-a", "s1", "tier:s1"), dep("s1-b", "s1", "tier:s1")]
    strat = DncRoutingStrategy(router=FakeRouter(deployments))
    assert _run(strat, "tier:s1")["model_info"]["dnc_squad"] == "s1"


def test_strategy_falls_back_to_model_list_without_get_model_list():
    deployments = [dep("s3-a", "s3"), dep("s1-a", "s1")]
    strat = DncRoutingStrategy(router=FakeRouter(deployments, with_get_model_list=False))
    assert _run(strat, "tier:auto", {"x-dnc-complexity": "low"})["model_info"]["dnc_squad"] == "s3"


def test_strategy_returns_none_when_group_empty():
    strat = DncRoutingStrategy(router=FakeRouter([dep("s1-a", "s1", "tier:s1")]))
    assert _run(strat, "tier:nonexistent") is None
