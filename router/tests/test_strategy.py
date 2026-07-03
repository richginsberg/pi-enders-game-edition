from dnc_router.strategy import (
    SPILL_THRESHOLD,
    DncRoutingStrategy,
    LoadTracker,
    pick_tier,
    select_deployment,
)


def dep(dep_id: str, squad: str) -> dict:
    return {"model_info": {"id": dep_id, "dnc_squad": squad}}


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

def test_strategy_filters_by_squad():
    import asyncio

    strat = DncRoutingStrategy()
    deployments = [dep("s1-a", "s1"), dep("s3-a", "s3"), dep("s3-b", "s3")]
    chosen = asyncio.run(
        strat.async_get_available_deployment(
            "tier:s3", deployments, messages=msgs("hello"), request_kwargs={}
        )
    )
    assert chosen["model_info"]["dnc_squad"] == "s3"
