from dnc_router.strategy import (
    SPILL_THRESHOLD,
    DncRoutingStrategy,
    LoadTracker,
    pick_tier,
    select_deployment,
    squad_for_deployment_id,
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


# --- health / cooldown filtering -------------------------------------------------

def test_unhealthy_host_excluded_from_affinity():
    candidates = [dep("a", "s3"), dep("b", "s3")]
    m = msgs("prefix that hashes to a")
    # find whichever host affinity prefers, then mark it unhealthy
    preferred = select_deployment(candidates, m, LoadTracker())["model_info"]["id"]
    other = "b" if preferred == "a" else "a"
    chosen = select_deployment(candidates, m, LoadTracker(), unhealthy_ids={preferred})
    assert chosen["model_info"]["id"] == other  # rehashed to the live sibling


def test_all_unhealthy_falls_back_to_full_set():
    candidates = [dep("a", "s3"), dep("b", "s3")]
    chosen = select_deployment(candidates, msgs("p"), LoadTracker(), unhealthy_ids={"a", "b"})
    assert chosen["model_info"]["id"] in {"a", "b"}  # last resort: don't black-hole


def test_unhealthy_excluded_from_spill_target():
    # preferred saturates and would spill, but the only other host is unhealthy -> stays put
    candidates = [dep("a", "s3"), dep("b", "s3")]
    tracker = LoadTracker()
    m = msgs("hot")
    pref = select_deployment(candidates, m, LoadTracker())["model_info"]["id"]
    other = "b" if pref == "a" else "a"
    seen = {
        select_deployment(candidates, m, tracker, now=1.0, unhealthy_ids={other})["model_info"]["id"]
        for _ in range(SPILL_THRESHOLD + 3)
    }
    assert seen == {pref}  # never spilled to the unhealthy host


def test_s3_spills_after_one_request_single_slot():
    # SQUAD_CONCURRENCY['s3']=1 -> a second request within the window spreads off the busy node
    deployments = [dep("s3-a", "s3"), dep("s3-b", "s3")]
    strat = DncRoutingStrategy(router=FakeRouter(deployments))
    seen = [_run(strat, "tier:auto", {"x-dnc-complexity": "low"}, text="same")["model_info"]["id"] for _ in range(2)]
    assert set(seen) == {"s3-a", "s3-b"}  # didn't dogpile one single-slot node


def test_strategy_skips_cooling_down_deployment():
    deployments = [dep("s3-a", "s3"), dep("s3-b", "s3")]
    router = FakeRouter(deployments)
    m = msgs("pick")
    pref = select_deployment(deployments, m, LoadTracker())["model_info"]["id"]
    router._get_cooldown_deployments = lambda: [pref]  # litellm cooled it down after failures
    strat = DncRoutingStrategy(router=router)
    chosen = _run(strat, "tier:auto", {"x-dnc-complexity": "low"}, text="pick")
    assert chosen["model_info"]["id"] != pref  # routed to the healthy sibling


def test_unhealthy_getter_errors_degrade_gracefully():
    router = FakeRouter([dep("s3-a", "s3")])
    def boom():
        raise RuntimeError("litellm internals changed")
    router._get_cooldown_deployments = boom
    strat = DncRoutingStrategy(router=router)
    # must not raise; falls back to no filtering
    assert _run(strat, "tier:auto", {"x-dnc-complexity": "low"})["model_info"]["id"] == "s3-a"


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


# --- squad_for_deployment_id (x-dnc-squad response header) --------------------
def test_squad_for_deployment_id_maps_back():
    members = [dep("s3-node-01", "s3", "tier:auto"), dep("glm-abc", "s0", "tier:auto")]
    assert squad_for_deployment_id(members, "s3-node-01") == "s3"
    assert squad_for_deployment_id(members, "glm-abc") == "s0"


def test_squad_for_deployment_id_unknown_returns_none():
    members = [dep("s3-node-01", "s3")]
    assert squad_for_deployment_id(members, "missing") is None
    assert squad_for_deployment_id([], "anything") is None


def test_squad_for_deployment_id_no_squad_returns_none():
    members = [{"model_info": {"id": "x"}}]  # deployment without dnc_squad
    assert squad_for_deployment_id(members, "x") is None
