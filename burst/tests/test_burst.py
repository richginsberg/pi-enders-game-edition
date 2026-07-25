import pytest

from burst import policy as P
from burst import profiles as PR
from burst import template as T


# -- capacity math (the numbers that decide --max-num-seqs) --------------------------
def test_kv_geometry_matches_published_configs():
    # Qwen3.6-27B: 64 layers, 3 linear : 1 full -> 16 full-attn, 4 kv heads, head_dim 256
    assert PR.QWEN36_27B.kv_bytes_per_token(1) == 32 * 1024          # 32 KiB/token @fp8
    assert PR.QWEN36_27B.kv_bytes_per_token(2) == 64 * 1024
    # 35B-A3B: 40 layers -> 10 full-attn, only 2 kv heads => 3.2x cheaper KV
    assert PR.QWEN36_35B_A3B.kv_bytes_per_token(1) == 10 * 1024
    assert PR.QWEN36_27B.kv_bytes_per_token(1) / PR.QWEN36_35B_A3B.kv_bytes_per_token(1) == 3.2


def test_full_context_kv_is_8gib_for_27b():
    # 262144 tokens x 32 KiB = 8 GiB — the reason long context is affordable at all
    assert 262144 * PR.QWEN36_27B.kv_bytes_per_token(1) == 8 * PR.GIB


def test_pro6000_plans_high_concurrency_not_two():
    p = PR.plan_capacity("rtxpro6000", "qwen3.6-27b", "autoround", context=262144)
    assert p.fits
    assert p.max_num_seqs == 8, p          # NOT the copy-pasted 2
    assert p.kv_pool_gib > 60


def test_moe_gets_far_more_concurrency_than_dense_on_same_card():
    dense = PR.plan_capacity("rtxpro6000", "qwen3.6-27b", "autoround", context=262144)
    moe = PR.plan_capacity("rtxpro6000", "qwen3.6-35b-a3b", "autoround", context=262144)
    assert moe.max_num_seqs > dense.max_num_seqs * 2


def test_3090_cannot_hold_full_context_dense():
    p = PR.plan_capacity("rtx3090", "qwen3.6-27b", "autoround", context=262144)
    assert not p.fits and "under one" in p.reason


def test_3090_works_at_reduced_context():
    p = PR.plan_capacity("rtx3090", "qwen3.6-27b", "autoround", context=32768)
    assert p.fits and p.max_num_seqs >= 4


def test_arch_gate_blocks_nvfp4_on_ampere():
    p = PR.plan_capacity("rtx3090", "qwen3.6-27b", "nvfp4", context=32768)
    assert not p.fits and "blackwell" in p.reason


def test_unknown_quant_is_reported_not_crashed():
    p = PR.plan_capacity("rtxpro6000", "qwen3.6-27b", "gguf", context=1024)
    assert not p.fits and "no 'gguf' quant" in p.reason


def test_rank_gpus_is_cheapest_first_and_filters_by_concurrency():
    plans = PR.rank_gpus("qwen3.6-35b-a3b", "autoround", context=32768, min_seqs=8)
    rates = [PR.GPUS[p.gpu].usd_per_hr for p in plans]
    assert rates == sorted(rates)
    assert all(p.max_num_seqs >= 8 for p in plans)


# -- the saturation gate --------------------------------------------------------------
ENABLED = P.Policy(enabled=True, per_node_tok_s=400)
ECON = P.Economics(gpu_usd_per_hr=1.69, expected_tok_s=800, fallback_usd_per_m_out=10.0)


def test_disabled_by_default():
    d = P.should_burst(P.Demand(50_000_000, 4), ECON, P.Policy())
    assert d.verdict is P.Verdict.NOT_CONFIGURED and not d.ok


def test_rejects_small_jobs_even_when_rate_is_good():
    d = P.should_burst(P.Demand(100_000, 4), ECON, ENABLED)
    assert not d.ok and "under the 5,000,000 minimum" in d.reason


def test_rejects_short_jobs():
    d = P.should_burst(P.Demand(50_000_000, 0.25), ECON, ENABLED)
    assert not d.ok and "under the 1.0h minimum" in d.reason


def test_rejects_low_saturation():
    d = P.should_burst(P.Demand(50_000_000, 4, expected_saturation=0.3), ECON, ENABLED)
    assert not d.ok and "saturation" in d.reason


def test_rejects_when_rate_cannot_beat_fallback():
    # cheap fallback (S3) — a rented Pro 6000 cannot reach it at any sane throughput
    econ = P.Economics(1.69, 800, 0.17)
    d = P.should_burst(P.Demand(500_000_000, 8), econ, ENABLED)
    assert not d.ok and "does not beat the fallback" in d.reason


def test_accepts_big_saturated_job_against_expensive_fallback():
    d = P.should_burst(P.Demand(50_000_000, 4), ECON, ENABLED)
    assert d.ok and d.replicas >= 1
    assert d.savings_usd > 0
    assert d.projected_cost_usd == pytest.approx(1.69 * 4 * d.replicas)


def test_respects_daily_cap():
    d = P.should_burst(P.Demand(50_000_000, 4), ECON, ENABLED, spent_today_usd=49.0)
    assert not d.ok and "daily cap" in d.reason


def test_replicas_scale_with_required_rate_and_cap():
    pol = P.Policy(enabled=True, per_node_tok_s=100, max_replicas=4)
    # 20M tokens in 1h = 5555 tok/s -> way over 4 nodes, so clamp
    assert P.plan_replicas(P.Demand(20_000_000, 1), pol) == 4
    # 720k tokens in 1h = 200 tok/s -> 2 nodes at 100 tok/s each
    assert P.plan_replicas(P.Demand(720_000, 1), pol) == 2


def test_economics_breakeven_matches_hand_math():
    e = P.Economics(1.69, 100, 0.17)
    assert e.effective_usd_per_m() == pytest.approx(4.694, abs=1e-3)
    assert e.breakeven_tok_s() == pytest.approx(2761, abs=1)
    assert not e.beats_fallback()


# -- lifecycle / reaper ---------------------------------------------------------------
def test_hard_lifetime_beats_activity():
    pol = P.Policy(enabled=True, max_lifetime_s=100, idle_ttl_s=900)
    act, why = P.lifecycle_action(P.NodeState("n", age_s=101, idle_s=0, inflight=3), pol)
    assert act is P.Action.KILL_EXPIRED and "max lifetime" in why


def test_inflight_requests_prevent_idle_kill():
    pol = P.Policy(enabled=True, idle_ttl_s=10, max_lifetime_s=10_000)
    act, _ = P.lifecycle_action(P.NodeState("n", age_s=50, idle_s=999, inflight=1), pol)
    assert act is P.Action.KEEP


def test_idle_ttl_scales_to_zero():
    pol = P.Policy(enabled=True, idle_ttl_s=10, max_lifetime_s=10_000)
    act, why = P.lifecycle_action(P.NodeState("n", age_s=50, idle_s=11, usd_per_hr=1.69), pol)
    assert act is P.Action.SCALE_TO_ZERO and "idle" in why


def test_volume_reaper_uses_its_own_clock():
    pol = P.Policy(enabled=True, volume_max_idle_days=14)
    kill, why = P.volume_action(P.VolumeState("v", 100, 20), pol)
    assert kill and "$7.00/mo" in why
    keep, _ = P.volume_action(P.VolumeState("v", 100, 3), pol)
    assert not keep


def test_spillover_goes_up_a_tier():
    assert P.spillover_tier("s3") == "s2"
    assert P.spillover_tier("s1") == "s0"
    assert P.spillover_tier("s0") is None


# -- templates -------------------------------------------------------------------------
def test_saturation_profile_computes_concurrency_from_the_card():
    t = T.render("qwen3.6-27b", "autoround", "rtxpro6000", context=262144)
    i = t.args.index("--max-num-seqs")
    assert t.args[i + 1] == str(t.plan.max_num_seqs) != "2"


def test_interactivity_profile_warns_that_it_is_wrong_for_rentals():
    t = T.render("qwen3.6-27b", "autoround", "rtxpro6000", profile=T.PROFILE_INTERACTIVITY)
    assert t.args[t.args.index("--max-num-seqs") + 1] == "2"
    assert any("WORST case for $/token" in w for w in t.warnings)


def test_blackwell_mtp_gets_the_cudagraph_workaround():
    t = T.render("qwen3.6-27b", "autoround", "rtxpro6000")
    assert "--speculative-config" in t.args
    assert '{"cudagraph_mode":"none"}' in t.args
    assert t.env["TORCH_CUDA_ARCH_LIST"] == "12.0"
    assert any("SM120" in w for w in t.warnings)


def test_ampere_gets_no_cudagraph_flag_and_no_fp4():
    t = T.render("qwen3.6-27b", "autoround", "rtx3090", context=32768)
    assert "--compilation-config" not in t.args
    # arch gate rejects it during capacity planning, before any launch args are built
    with pytest.raises(ValueError, match="needs blackwell"):
        T.render("qwen3.6-27b", "nvfp4", "rtx3090", context=32768)


def test_refuses_nvfp4_moe_on_sm120():
    # vLLM #35065 closed as not planned — fail loudly rather than debug at $1.69/hr
    PR.QWEN36_35B_A3B.quants["nvfp4"] = PR.QuantProfile(
        "nvfp4", "x/y", 20.0, "modelopt_fp4", "blackwell")
    try:
        with pytest.raises(ValueError, match="NVFP4 MoE on SM120"):
            T.render("qwen3.6-35b-a3b", "nvfp4", "rtxpro6000", context=32768)
    finally:
        PR.QWEN36_35B_A3B.quants.pop("nvfp4")


def test_no_secret_is_ever_baked_into_a_template():
    t = T.render("qwen3.6-27b", "autoround", "rtxpro6000")
    assert t.args[t.args.index("--api-key") + 1].startswith("$")


def test_single_24gb_dense_warns_about_the_prefill_cliff():
    t = T.render("qwen3.6-27b", "autoround", "rtx3090", context=32768)
    assert any("prefill cliff" in w for w in t.warnings)


def test_mtp_absent_when_the_quant_cannot_support_it():
    t = T.render("qwen3.6-27b", "nvfp4", "rtxpro6000")
    assert "--speculative-config" not in t.args
    assert any("draft acceptance silently drops to 0%" in w for w in t.warnings)


def test_best_template_picks_the_cheapest_card_that_batches():
    t = T.best_template("qwen3.6-35b-a3b", context=32768, min_seqs=8)
    assert t.plan.max_num_seqs >= 8
    assert PR.GPUS[t.gpu].usd_per_hr <= PR.GPUS["rtxpro6000"].usd_per_hr


def test_every_emitted_switch_is_documented():
    t = T.render("qwen3.6-27b", "autoround", "rtxpro6000")
    emitted = {a for a in t.args if a.startswith("--")}
    assert emitted <= set(T.SWITCHES), emitted - set(T.SWITCHES)
