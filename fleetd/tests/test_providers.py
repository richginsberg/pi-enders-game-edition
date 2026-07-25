import os

import pytest
import yaml

from fleetd import providers as P


def test_every_provider_has_a_catalog_covering_all_tiers():
    for key in P.PROVIDERS:
        cat = P.CATALOG.get(key)
        assert cat, f"{key} has no catalog"
        for tier in P.TIERS:
            assert cat.get(tier), f"{key} has no {tier} options"


def test_every_tier_has_exactly_one_default():
    for prov, cat in P.CATALOG.items():
        for tier, picks in cat.items():
            n = sum(1 for p in picks if p.get("default"))
            assert n <= 1, f"{prov}/{tier} has {n} defaults"
            assert P.defaults_for(prov).get(tier), f"{prov}/{tier} yields no default"


def test_openweight_only_providers_are_flagged():
    # DeepInfra cannot serve Claude/GPT — the UI needs to say so before a user picks it
    assert P.PROVIDERS["deepinfra"].has_closed_models is False
    assert P.PROVIDERS["openrouter"].has_closed_models is True
    for tier_picks in P.CATALOG["deepinfra"].values():
        for pick in tier_picks:
            assert "claude" not in pick["model"].lower()
            assert not pick["model"].lower().startswith("openai/gpt-5")


# -- tiers.yaml round trip -------------------------------------------------------------
def test_load_defaults_when_no_file(tmp_path):
    cfg = P.load_tiers(str(tmp_path / "nope.yaml"))
    assert cfg["provider"] == "openrouter"
    assert set(cfg["tiers"]) == set(P.TIERS)


def test_save_then_load_round_trips(tmp_path):
    p = str(tmp_path / "tiers.yaml")
    cfg = {"provider": "deepinfra", "tiers": P.defaults_for("deepinfra")}
    P.save_tiers(cfg, p)
    assert P.load_tiers(p) == cfg


def test_validate_catches_bad_input():
    assert P.validate({"provider": "nope", "tiers": P.defaults_for("openrouter")})
    assert P.validate({"provider": "openrouter", "tiers": {"s0": "m"}})  # missing tiers
    assert not P.validate({"provider": "openrouter", "tiers": P.defaults_for("openrouter")})


# -- rendering into the LiteLLM config ---------------------------------------------------
HAND_MANAGED = """\
# my notes
model_list:
  - model_name: embed:qwen3
    litellm_params: { model: openai/x, api_base: http://h/v1, api_key: none }
    model_info: { dnc_squad: s3, mode: embedding }
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
"""


def _cfg(provider="openrouter"):
    return {"provider": provider, "tiers": P.defaults_for(provider)}


def test_apply_adds_all_tiers_and_tier_auto():
    out = P.apply_to_config(HAND_MANAGED, _cfg())
    ml = yaml.safe_load(out)["model_list"]
    groups = [m["model_name"] for m in ml]
    for tier in P.TIERS:
        assert f"tier:{tier}" in groups
    autos = [m for m in ml if m["model_name"] == "tier:auto"]
    assert {m["model_info"]["dnc_squad"] for m in autos} == set(P.TIERS)


def test_apply_preserves_hand_managed_entries_and_comments():
    out = P.apply_to_config(HAND_MANAGED, _cfg())
    assert "# my notes" in out
    assert "master_key" in out
    assert any(m["model_name"] == "embed:qwen3" for m in yaml.safe_load(out)["model_list"])


def test_apply_is_idempotent():
    once = P.apply_to_config(HAND_MANAGED, _cfg())
    assert P.apply_to_config(once, _cfg()) == once


def test_apply_replaces_rather_than_duplicating_on_provider_switch():
    once = P.apply_to_config(HAND_MANAGED, _cfg("openrouter"))
    twice = P.apply_to_config(once, _cfg("deepinfra"))
    ml = yaml.safe_load(twice)["model_list"]
    s0 = [m for m in ml if m["model_name"] == "tier:s0"]
    assert len(s0) == 1                      # not duplicated
    assert "deepinfra" in s0[0]["litellm_params"]["api_base"]


def test_rendered_entries_carry_prices_and_billing_for_the_usage_tracker():
    out = P.apply_to_config(HAND_MANAGED, _cfg())
    s3 = [m for m in yaml.safe_load(out)["model_list"] if m["model_name"] == "tier:s3"][0]
    mi = s3["model_info"]
    assert mi["dnc_billing"] == "per_token"
    # gpt-oss-120b is $0.037/M in -> 3.7e-8 per token
    assert mi["input_cost_per_token"] == pytest.approx(0.037 / 1e6)


def test_apply_never_inlines_a_key():
    out = P.apply_to_config(HAND_MANAGED, _cfg())
    assert "os.environ/OPENROUTER_API_KEY" in out
    assert "sk-" not in out


def test_apply_bootstraps_an_empty_config():
    out = P.apply_to_config("model_list:\n", _cfg())
    assert len(yaml.safe_load(out)["model_list"]) == 8   # 4 tiers + 4 tier:auto


# -- secrets ------------------------------------------------------------------------------
def test_set_env_var_creates_chmod_600(tmp_path):
    p = str(tmp_path / ".env")
    P.set_env_var("OPENROUTER_API_KEY", "sk-or-secret", p)
    assert open(p).read().strip() == "OPENROUTER_API_KEY=sk-or-secret"
    assert oct(os.stat(p).st_mode)[-3:] == "600"


def test_set_env_var_replaces_in_place_and_keeps_others(tmp_path):
    p = str(tmp_path / ".env")
    open(p, "w").write("KEEP=1\nOPENROUTER_API_KEY=old\nexport OTHER=2\n")
    P.set_env_var("OPENROUTER_API_KEY", "new", p)
    body = open(p).read()
    assert "OPENROUTER_API_KEY=new" in body and "old" not in body
    assert "KEEP=1" in body and "export OTHER=2" in body


def test_set_env_var_matches_an_exported_form(tmp_path):
    p = str(tmp_path / ".env")
    open(p, "w").write("export OPENROUTER_API_KEY=old\n")
    P.set_env_var("OPENROUTER_API_KEY", "new", p)
    assert open(p).read().count("OPENROUTER_API_KEY") == 1
    assert "old" not in open(p).read()


def test_set_env_var_rejects_injection():
    with pytest.raises(ValueError):
        P.set_env_var("BAD NAME", "x", "/tmp/x.env")
    with pytest.raises(ValueError):
        P.set_env_var("OPENROUTER_API_KEY", "line1\nEVIL=2", "/tmp/x.env")


def test_has_env_var_needs_a_value(tmp_path):
    p = str(tmp_path / ".env")
    open(p, "w").write("EMPTY=\nSET=x\n")
    assert P.has_env_var("SET", p)
    assert not P.has_env_var("EMPTY", p)
    assert not P.has_env_var("ABSENT", p)


def test_mask_never_reveals_the_middle():
    m = P.mask("sk-or-v1-0123456789abcdef")
    assert m.startswith("sk-o") and m.endswith("cdef") and "0123456789" not in m
    assert P.mask("short") == "*****"
