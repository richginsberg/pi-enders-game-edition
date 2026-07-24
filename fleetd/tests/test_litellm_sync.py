import pytest
import yaml

from fleetd import litellm_sync as ls


# A config that mirrors the real one: hand-managed s1/s0 (+ a disabled-grok comment) and
# general_settings around a block of generated s3 node entries (both tier:s3 and tier:auto).
SAMPLE = """\
# dnc LiteLLM — LOCAL config
model_list:
  - model_name: tier:s1
    litellm_params: { model: openai/stepfun, api_base: os.environ/DNC_S1_API_BASE, api_key: sk-x }
    model_info: { dnc_squad: s1, max_input_tokens: 262144, max_output_tokens: 8192 }
  # --- grok DISABLED (re-enable with a valid xai- key) ---
  #   - model_name: tier:s0
  #     litellm_params: { model: grok, api_key: sk-grok }
  - model_name: tier:s0
    litellm_params: { model: os.environ/DNC_S0_MODEL, api_base: https://api.z.ai/api/paas/v4, api_key: sk-glm }
    model_info: { dnc_squad: s0, max_input_tokens: 200000, max_output_tokens: 131072 }
  - model_name: tier:s3
    litellm_params: { model: openai/qwen3.6-35b-reap, api_base: http://192.168.1.101:8080/v1, api_key: sk-noauth }
    model_info: { id: s3-node-01, dnc_squad: s3, max_input_tokens: 262144, max_output_tokens: 8192 }
  - model_name: tier:auto
    litellm_params: { model: openai/qwen3.6-35b-reap, api_base: http://192.168.1.101:8080/v1, api_key: sk-noauth }
    model_info: { id: s3-node-01, dnc_squad: s3, max_input_tokens: 262144, max_output_tokens: 8192 }
  - model_name: tier:auto
    litellm_params: { model: os.environ/DNC_S0_MODEL, api_base: https://api.z.ai/api/paas/v4, api_key: sk-glm }
    model_info: { dnc_squad: s0 }
general_settings:
  master_key: sk-master
  database_url: os.environ/DATABASE_URL
"""

NODES = {
    "bc25001": {"ip": "192.168.1.150", "tier": "s3"},
    "bc25002": {"ip": "192.168.1.151", "tier": "s3", "port": 9090},
}


def _models(text):
    return yaml.safe_load(text)["model_list"]


def test_rewrite_generates_two_entries_per_node_with_name_ids():
    out = ls.rewrite_config(SAMPLE, NODES)
    parsed = _models(out)
    node = [m for m in parsed if (m.get("model_info") or {}).get("dnc_squad") == "s3"
            and str(m["litellm_params"]["api_base"]).startswith("http://")]
    assert len(node) == 4  # 2 nodes x (tier:s3 + tier:auto)
    ids = {m["model_info"]["id"] for m in node}
    assert ids == {"s3-bc25001", "s3-bc25002"}  # ids track node names, not s3-node-NN
    # per-node port override honored
    bases = {m["litellm_params"]["api_base"] for m in node}
    assert "http://192.168.1.150:8080/v1" in bases and "http://192.168.1.151:9090/v1" in bases


def test_rewrite_preserves_hand_managed_entries_and_comment():
    out = ls.rewrite_config(SAMPLE, NODES)
    # s1 + s0(tier:s0) + s0(tier:auto) all survive; grok comment survives verbatim
    parsed = _models(out)
    squads = sorted((m.get("model_info") or {}).get("dnc_squad") for m in parsed
                    if (m.get("model_info") or {}).get("dnc_squad") in ("s0", "s1"))
    assert squads == ["s0", "s0", "s1"]
    assert "grok DISABLED" in out
    assert "master_key: sk-master" in out  # general_settings untouched


def test_rewrite_is_idempotent():
    once = ls.rewrite_config(SAMPLE, NODES)
    twice = ls.rewrite_config(once, NODES)
    assert once == twice  # markers make re-runs stable


def test_rewrite_reflects_ip_change_on_rebuild():
    out1 = ls.rewrite_config(SAMPLE, NODES)
    moved = {**NODES, "bc25001": {"ip": "192.168.1.199", "tier": "s3"}}  # rebuilt, new DHCP ip
    out2 = ls.rewrite_config(out1, moved)
    bases = {m["litellm_params"]["api_base"] for m in _models(out2)
             if (m.get("model_info") or {}).get("id") == "s3-bc25001"}
    assert bases == {"http://192.168.1.199:8080/v1"}


def test_rewrite_derives_template_from_existing_entry():
    # api_key/model come from the existing node entry, not hardcoded
    out = ls.rewrite_config(SAMPLE, NODES)
    node = [m for m in _models(out) if (m.get("model_info") or {}).get("id") == "s3-bc25001"][0]
    assert node["litellm_params"]["model"] == "openai/qwen3.6-35b-reap"
    assert node["litellm_params"]["api_key"] == "sk-noauth"


def test_rewrite_rejects_config_without_model_list():
    with pytest.raises(ValueError, match="model_list"):
        ls.rewrite_config("general_settings:\n  master_key: x\n", NODES)


def test_sync_writes_backup_and_can_skip_restart(tmp_path):
    p = tmp_path / "litellm-config.yaml"
    p.write_text(SAMPLE)
    res = ls.sync(str(p), {"nodes": {**NODES, "gw": {"ip": "1.2.3.4", "tier": "s1"}}}, restart=False)
    assert res["entries"] == 4 and res["s3_nodes"] == ["bc25001", "bc25002"]  # s1 node excluded
    assert res["restarted"] is False
    assert (tmp_path / "litellm-config.yaml.bak-litellmsync").read_text() == SAMPLE  # backup is the old file
    assert "s3-bc25001" in p.read_text()
