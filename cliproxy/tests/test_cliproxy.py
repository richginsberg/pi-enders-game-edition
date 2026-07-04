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


def test_grok_headers_from_auth_json(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"grok-cli": {"type": "api_key", "key": "ey.JWT.tok"}}))
    g = GrokModule(auth_json=str(auth), model_map={"grok-composer-2.5-fast": "composer-2.5"})
    h = g.headers({"model": "grok-composer-2.5-fast"})
    assert h["authorization"] == "Bearer ey.JWT.tok"     # token read fresh from auth.json
    assert h["x-xai-token-auth"] == "xai-grok-cli"
    assert h["x-grok-model-override"] == "composer-2.5"   # mapped to upstream id
    assert g.owns("grok-build")                           # default model set


def test_grok_defaults():
    g = GrokModule()
    ids = {e["id"] for e in g.models()}
    assert "grok-composer-2.5-fast" in ids and "grok-build" in ids
