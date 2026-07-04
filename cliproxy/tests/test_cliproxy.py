import json
import os

from cliproxy.modules.grok import GrokModule
from cliproxy.modules.passthrough import OpenAIPassthrough
from cliproxy.registry import build_modules, find_module


def test_passthrough_catalog_and_ownership():
    m = OpenAIPassthrough("p", "https://x/v1", api_key="k", model_ids=["a", "b"])
    assert {e["id"] for e in m.models()} == {"a", "b"}
    assert m.owns("a") and not m.owns("z")
    assert [e["owned_by"] for e in m.models()] == ["p", "p"]


def test_passthrough_headers_and_model_map():
    m = OpenAIPassthrough("p", "https://x/v1/", api_key="k", model_ids=["exposed"],
                          model_map={"exposed": "upstream-id"}, extra_headers={"x-foo": "1"})
    h = m.headers()
    assert h["authorization"] == "Bearer k" and h["x-foo"] == "1"
    assert m._url() == "https://x/v1/chat/completions"  # trailing slash stripped
    assert m._upstream_body({"model": "exposed", "stream": False})["model"] == "upstream-id"


def test_passthrough_none_key_omits_auth():
    m = OpenAIPassthrough("p", "https://x/v1", api_key="none", model_ids=["a"])
    assert "authorization" not in m.headers()


def test_registry_env_expand_and_routing(monkeypatch):
    monkeypatch.setenv("MYKEY", "secret123")
    mods = build_modules({"modules": [
        {"type": "passthrough", "name": "d", "base_url": "https://d/v1",
         "api_key": "${MYKEY}", "model_ids": ["m1"]},
    ]})
    assert mods[0].api_key == "secret123"          # ${VAR} expanded
    assert find_module(mods, "m1") is mods[0]
    assert find_module(mods, "nope") is None


def test_grok_headers_from_oidc_auth_json(tmp_path):
    # real grok-cli auth.json shape: keyed by <oidc_issuer>::<client_id>
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"https://auth.x.ai::cid-123": {
        "key": "ey.JWT.tok", "refresh_token": "rt", "oidc_issuer": "https://auth.x.ai",
        "oidc_client_id": "cid-123", "expires_at": "2099-01-01T00:00:00Z",
    }}))
    g = GrokModule(auth_json=str(auth), refresh=False,
                   model_map={"grok-composer-2.5-fast": "composer-2.5"},
                   gated_headers={"x-grok-client-version": "0.0.0"})
    h = g.headers({"model": "grok-composer-2.5-fast"})
    assert h["authorization"] == "Bearer ey.JWT.tok"      # token from the OIDC entry's `key`
    assert h["x-xai-token-auth"] == "xai-grok-cli"
    assert h["x-grok-model-override"] == "composer-2.5"    # mapped to upstream id
    assert h["x-grok-client-version"] == "0.0.0"           # gated headers pass through
    assert g.owns("grok-build")


def test_grok_expiry_detection(tmp_path):
    g = GrokModule(auth_json=str(tmp_path / "x"))
    assert g._expired({"expires_at": "2000-01-01T00:00:00Z"}) is True
    assert g._expired({"expires_at": "2099-01-01T00:00:00Z"}) is False
    assert g._expired({}) is False  # no expiry -> treat as valid


def test_grok_defaults():
    g = GrokModule()
    ids = {e["id"] for e in g.models()}
    assert "grok-composer-2.5-fast" in ids and "grok-build" in ids
