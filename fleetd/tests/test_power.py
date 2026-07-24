import pytest

from fleetd import power


CFG = {
    "_path": "/x/fleet-nodes.yaml",
    "broadcast": "192.168.1.255",
    "ssh_user": "svc",
    "nodes": {
        "bc25001": {"ip": "192.168.1.123", "mac": "aa:bb:cc:dd:ee:01", "tier": "s3"},
        "bc25002": {"ip": "192.168.1.168", "mac": "aa:bb:cc:dd:ee:02", "tier": "s3"},
        "bc25006": {"ip": "192.168.1.118", "mac": "aa:bb:cc:dd:ee:06", "tier": "s3", "never_sleep": True},
        "gw-s1":   {"ip": "192.168.1.50",  "mac": "aa:bb:cc:dd:ee:50", "tier": "s1"},
    },
}


# -- selection ----------------------------------------------------------------------
def test_norm_helpers():
    assert power.norm_name("1") == "bc25001"
    assert power.norm_name("bc25005") == "bc25005"
    assert power.norm_ip(".106") == "192.168.1.106"
    assert power.norm_ip("10.0.0.4") == "10.0.0.4"


def test_select_by_tier_is_sorted_and_scoped():
    got = power.select_targets(CFG, tier="s3")
    assert [n for n, _ in got] == ["bc25001", "bc25002", "bc25006"]  # sorted, s1 excluded


def test_select_union_of_selectors_dedups():
    got = power.select_targets(CFG, tier="s1", nodes="1", ips=".168")
    assert sorted(n for n, _ in got) == ["bc25001", "bc25002", "gw-s1"]


def test_select_ignores_unknown_nodes():
    assert power.select_targets(CFG, nodes="999,bc25001") == [("bc25001", CFG["nodes"]["bc25001"])]


def test_partition_never_sleep_holds_back_unless_forced():
    targets = power.select_targets(CFG, tier="s3")
    act, skip = power.partition_never_sleep(targets, force=False)
    assert [n for n, _ in act] == ["bc25001", "bc25002"]
    assert [n for n, _ in skip] == ["bc25006"]
    act2, skip2 = power.partition_never_sleep(targets, force=True)
    assert len(act2) == 3 and skip2 == []


# -- state machine ------------------------------------------------------------------
def test_phase_on_progression():
    assert power.phase_on(ssh_up=False, health_code=None) == "booting"
    assert power.phase_on(ssh_up=True, health_code=None) == "loading"   # booted, not ready
    assert power.phase_on(ssh_up=True, health_code=503) == "loading"    # port up, unhealthy
    assert power.phase_on(ssh_up=True, health_code=200) == "serving"


def test_phase_off_progression():
    assert power.phase_off(ssh_up=True, health_code=200) == "stopping"
    assert power.phase_off(ssh_up=True, health_code=None) == "stopping"
    assert power.phase_off(ssh_up=False, health_code=None) == "offline"


def test_eta_counts_down_and_zeroes_on_terminal():
    assert power.eta_s("loading", elapsed=30, budget=90) == 60
    assert power.eta_s("loading", elapsed=200, budget=90) == 0   # clamped
    assert power.eta_s("serving", elapsed=10, budget=90) == 0    # terminal
    assert power.eta_s("offline", elapsed=5, budget=45) == 0


def test_summarize_counts_by_state():
    states = {"a": "serving", "b": "loading", "c": "serving", "d": "timeout"}
    s = power.summarize(states, "on", elapsed=42.0)
    assert (s["total"], s["done"], s["timeout"], s["pending"]) == (4, 2, 1, 1)


# -- the poll loop with injected probes (no real network) ---------------------------
@pytest.mark.asyncio
async def test_track_node_on_reaches_serving_and_emits_phase_changes():
    # Scripted probe results over successive polls: down -> ssh up/not-ready -> serving.
    # health_probe only fires when ssh is up, so it needs one entry per ssh-up poll.
    ssh_seq = iter([False, True, True])
    health_seq = iter([None, 200])
    clock = iter([0.0, 0.0, 5.0, 40.0])  # start + one read per iteration

    async def fake_ssh(ip, port, timeout=2.0):
        return next(ssh_seq)

    async def fake_health(ip, port, timeout=3.0):
        return next(health_seq)

    events = []
    await power.track_node(
        "bc25001", CFG["nodes"]["bc25001"], "on",
        budget_s=90, poll_s=0, emit=events.append,
        now=lambda: next(clock), ssh_probe=fake_ssh, health_probe=fake_health,
    )
    phases = [e["phase"] for e in events]
    assert phases[0] == "waking"                 # seeded intent
    assert phases[-1] == "serving"               # terminal
    assert "booting" in phases and "loading" in phases  # went through the machine
    assert events[-1]["eta_s"] == 0


@pytest.mark.asyncio
async def test_track_node_times_out_if_never_serves():
    async def fake_ssh(ip, port, timeout=2.0):
        return True

    async def fake_health(ip, port, timeout=3.0):
        return 503  # never healthy

    clock = iter([0.0, 100.0, 100.0])  # start, then already past a 90s budget
    events = []
    await power.track_node(
        "bc25002", CFG["nodes"]["bc25002"], "on",
        budget_s=90, poll_s=0, emit=events.append,
        now=lambda: next(clock), ssh_probe=fake_ssh, health_probe=fake_health,
    )
    assert events[-1]["phase"] == "timeout"


@pytest.mark.asyncio
async def test_track_node_off_reaches_offline():
    ssh_seq = iter([True, False])
    async def fake_ssh(ip, port, timeout=2.0):
        return next(ssh_seq)

    async def fake_health(ip, port, timeout=3.0):
        return None

    clock = iter([0.0, 1.0, 1.0, 3.0, 3.0])
    events = []
    await power.track_node(
        "bc25001", CFG["nodes"]["bc25001"], "off",
        budget_s=45, poll_s=0, emit=events.append,
        now=lambda: next(clock), ssh_probe=fake_ssh, health_probe=fake_health,
    )
    assert events[0]["phase"] == "stopping"
    assert events[-1]["phase"] == "offline"
