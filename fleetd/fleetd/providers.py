"""Provider + tier configuration, driven from Pi instead of a text editor.

Setting up the gateway used to mean: hand-edit `litellm-config.yaml`, know that the API key
has to land in `~/dnc/.env` (not just your shell, or the systemd unit won't see it), then
restart. That's a bad on-ramp. This module makes provider choice, key entry and per-tier
model selection declarative so `/fleet-setup` can drive the whole thing.

Source of truth is a tiny file, `~/dnc/tiers.yaml`:

    provider: openrouter
    tiers: {s0: anthropic/claude-opus-5, s1: ..., s2: ..., s3: ...}

which renders into a **marker-fenced block** of the LiteLLM config — the same technique
`litellm_sync` uses for node entries, so the two compose and anything you hand-manage
outside the markers survives untouched.

Keys are written to `~/dnc/.env` at chmod 600 and are NEVER logged, echoed or returned by
the API.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import yaml

MARK_START = "  # >>> dnc-managed cloud tiers (fleetd /fleet-setup — do not edit by hand) >>>"
MARK_END = "  # <<< dnc-managed cloud tiers <<<"
TIERS = ("s0", "s1", "s2", "s3")


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    api_base: str
    key_env: str
    signup: str
    #: Closed frontier models are API-exclusive; open-weight-only providers cannot serve them.
    has_closed_models: bool
    note: str = ""


PROVIDERS: dict[str, Provider] = {
    "openrouter": Provider(
        "openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
        "https://openrouter.ai/keys", True,
        "One key for every frontier + open model. Cheapest source for open weights in our "
        "comparisons (4x under Together/Fireworks/Groq on identical models)."),
    "deepinfra": Provider(
        "deepinfra", "DeepInfra", "https://api.deepinfra.com/v1/openai", "DEEPINFRA_API_KEY",
        "https://deepinfra.com/dash/api_keys", False,
        "Open weights only — no Claude/GPT. Very cheap, and the natural pick if you want "
        "every tier on open models. No free tier (floor ~$0.019/M)."),
    "groq": Provider(
        "groq", "Groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY",
        "https://console.groq.com/keys", False,
        "Fastest published throughput (~1000 tok/s on gpt-oss-20b) at ~2.5x OpenRouter's "
        "price for the same weights. Best as a latency override, not a bulk default."),
    "together": Provider(
        "together", "Together AI", "https://api.together.xyz/v1", "TOGETHER_API_KEY",
        "https://api.together.ai/settings/api-keys", False,
        "Open weights; consistently the most expensive in our comparisons."),
}

#: Curated per-tier choices. Prices are $/M (in, out), verified 2026-07-25 — they drift.
#: `default=True` marks the shipped pick for that tier.
CATALOG: dict[str, dict[str, list[dict]]] = {
    "openrouter": {
        "s0": [
            {"model": "anthropic/claude-opus-5", "in": 5.00, "out": 25.00, "default": True,
             "note": "strong agentic/subagent coordination"},
            {"model": "openai/gpt-5.6-sol", "in": 5.00, "out": 30.00,
             "note": "OpenAI flagship"},
            {"model": "moonshotai/kimi-k3", "in": 3.00, "out": 15.00,
             "note": "cheapest frontier-class; open weights"},
            {"model": "anthropic/claude-fable-5", "in": 10.00, "out": 50.00,
             "note": "long-horizon autonomous; no ZDR option"},
        ],
        "s1": [
            {"model": "anthropic/claude-sonnet-5", "in": 2.00, "out": 10.00, "default": True,
             "note": "best price/perf in the Anthropic line"},
            {"model": "deepseek/deepseek-v4-pro", "in": 0.435, "out": 0.87,
             "note": "outrageous price/capability; open weights"},
            {"model": "z-ai/glm-5.2", "in": 0.7616, "out": 2.3936, "note": "cheap for its class"},
            {"model": "x-ai/grok-4.5", "in": 2.00, "out": 6.00, "note": "500K context"},
            {"model": "openai/gpt-5.6-terra", "in": 2.50, "out": 15.00, "note": "mid GPT-5.6"},
        ],
        "s2": [
            {"model": "deepseek/deepseek-v4-flash", "in": 0.094, "out": 0.188, "default": True,
             "note": "best price/capability on the list; 1M context"},
            {"model": "qwen/qwen3-coder-next", "in": 0.110, "out": 0.800,
             "note": "best sub-$0.15 coding model"},
            {"model": "qwen/qwen3.5-flash-02-23", "in": 0.065, "out": 0.260, "note": "1M context"},
            {"model": "z-ai/glm-4.5-air", "in": 0.130, "out": 0.850, "note": "proven agentic coder"},
        ],
        "s3": [
            {"model": "openai/gpt-oss-120b", "in": 0.037, "out": 0.170, "default": True,
             "note": "standout value — near-20b price, much stronger"},
            {"model": "openai/gpt-oss-20b:free", "in": 0.0, "out": 0.0,
             "note": "FREE, rate-limited — pair with a paid fallback"},
            {"model": "qwen/qwen3-coder-30b-a3b-instruct", "in": 0.070, "out": 0.270,
             "note": "writes files to spec well"},
            {"model": "openai/gpt-oss-20b", "in": 0.030, "out": 0.130, "note": "reliable tool calls"},
        ],
    },
    # DeepInfra hosts open weights only, so its "s0" is the strongest open model rather than
    # a frontier one. Slugs follow DeepInfra's org/Model convention; confirm on their model
    # page if one 404s — `/fleet-setup` probes each tier after applying so you find out fast.
    "deepinfra": {
        "s0": [
            {"model": "deepseek-ai/DeepSeek-V4-Pro", "in": 1.74, "out": 3.48, "default": True,
             "note": "strongest open model here (OpenRouter is ~4x cheaper for it)"},
            {"model": "Qwen/Qwen3-235B-A22B-Instruct-2507", "in": 0.09, "out": 0.55,
             "note": "big-model quality, S2 price"},
        ],
        "s1": [
            {"model": "Qwen/Qwen3-235B-A22B-Instruct-2507", "in": 0.09, "out": 0.55,
             "default": True, "note": "excellent value at this tier"},
            {"model": "zai-org/GLM-4.7-Flash", "in": 0.06, "out": 0.40,
             "note": "strong agentic/tool use; cached input $0.01"},
        ],
        "s2": [
            {"model": "deepseek-ai/DeepSeek-V4-Flash", "in": 0.09, "out": 0.18, "default": True,
             "note": "cached input $0.018"},
            {"model": "Qwen/Qwen3-32B", "in": 0.08, "out": 0.28, "note": "cheap dense reasoner"},
        ],
        "s3": [
            {"model": "zai-org/GLM-4.7-Flash", "in": 0.06, "out": 0.40, "default": True,
             "note": "30B-A3B MoE; good bulk coder"},
            {"model": "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo", "in": 0.02, "out": 0.04,
             "note": "trivial/classification work only"},
            {"model": "mistralai/Mistral-Nemo-Instruct-2407", "in": 0.019, "out": 0.03,
             "note": "cheapest here; too weak for real code"},
        ],
    },
}
CATALOG["groq"] = {
    "s0": [{"model": "openai/gpt-oss-120b", "in": 0.15, "out": 0.60, "default": True,
            "note": "500 tok/s"}],
    "s1": [{"model": "openai/gpt-oss-120b", "in": 0.15, "out": 0.60, "default": True}],
    "s2": [{"model": "openai/gpt-oss-20b", "in": 0.075, "out": 0.30, "default": True,
            "note": "~1000 tok/s"}],
    "s3": [{"model": "llama-3.1-8b-instant", "in": 0.05, "out": 0.08, "default": True,
            "note": "840 tok/s; weak at code"}],
}
CATALOG["together"] = CATALOG["openrouter"]  # same open models, pricier; slugs differ — verify


def defaults_for(provider: str) -> dict[str, str]:
    cat = CATALOG.get(provider) or {}
    out = {}
    for tier in TIERS:
        picks = cat.get(tier) or []
        chosen = next((p for p in picks if p.get("default")), picks[0] if picks else None)
        if chosen:
            out[tier] = chosen["model"]
    return out


def price_of(provider: str, tier: str, model: str) -> tuple[float, float] | None:
    for p in (CATALOG.get(provider) or {}).get(tier, []):
        if p["model"] == model:
            return p["in"], p["out"]
    return None


# --- tiers.yaml -----------------------------------------------------------------------
def tiers_path() -> str:
    return os.environ.get("DNC_TIERS_FILE", os.path.expanduser("~/dnc/tiers.yaml"))


def load_tiers(path: str | None = None) -> dict:
    p = path or tiers_path()
    if not os.path.exists(p):
        return {"provider": "openrouter", "tiers": defaults_for("openrouter")}
    with open(p) as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("provider", "openrouter")
    cfg.setdefault("tiers", defaults_for(cfg["provider"]))
    return cfg


def save_tiers(cfg: dict, path: str | None = None) -> str:
    p = path or tiers_path()
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = f"{p}.tmp"
    with open(tmp, "w") as f:
        f.write("# Managed by fleetd (/fleet-setup). Declares which model backs each tier.\n")
        yaml.safe_dump(cfg, f, sort_keys=False)
    os.replace(tmp, p)
    return p


def validate(cfg: dict) -> list[str]:
    errs = []
    prov = cfg.get("provider")
    if prov not in PROVIDERS:
        errs.append(f"unknown provider {prov!r} (have {sorted(PROVIDERS)})")
    for tier, model in (cfg.get("tiers") or {}).items():
        if tier not in TIERS:
            errs.append(f"unknown tier {tier!r}")
        if not model or not isinstance(model, str):
            errs.append(f"tier {tier} has no model")
    missing = [t for t in TIERS if t not in (cfg.get("tiers") or {})]
    if missing:
        errs.append(f"tiers missing a model: {', '.join(missing)}")
    return errs


# --- rendering into the LiteLLM config --------------------------------------------------
def _entry(model_name: str, provider: Provider, model: str, tier: str,
           price: tuple[float, float] | None, dep_id: str) -> list[str]:
    info = [f"id: {dep_id}", f"dnc_squad: {tier}", "dnc_billing: per_token"]
    if price:
        info += [f"input_cost_per_token: {price[0] / 1e6:.12g}",
                 f"output_cost_per_token: {price[1] / 1e6:.12g}"]
    return [
        f"  - model_name: {model_name}",
        f"    litellm_params: {{ model: openai/{model}, api_base: {provider.api_base}, "
        f"api_key: os.environ/{provider.key_env} }}",
        f"    model_info: {{ {', '.join(info)} }}",
    ]


def render_block(cfg: dict) -> list[str]:
    """The marker-fenced cloud-tier block: explicit tier:sN entries plus one tier:auto
    member per squad (which is what makes complexity routing resolvable)."""
    provider = PROVIDERS[cfg["provider"]]
    out = [MARK_START]
    for tier in TIERS:
        model = cfg["tiers"].get(tier)
        if not model:
            continue
        price = price_of(cfg["provider"], tier, model)
        slug = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")[:32]
        out += _entry(f"tier:{tier}", provider, model, tier, price, f"{tier}-{slug}")
    for tier in TIERS:
        model = cfg["tiers"].get(tier)
        if not model:
            continue
        price = price_of(cfg["provider"], tier, model)
        out += _entry("tier:auto", provider, model, tier, price, f"auto-{tier}")
    out.append(MARK_END)
    return out


def apply_to_config(text: str, cfg: dict) -> str:
    """Replace (or insert) the managed cloud-tier block. Everything outside the markers —
    including a node block managed by litellm_sync — is preserved verbatim."""
    lines = text.split("\n")
    # strip any previous managed block
    kept, skip = [], False
    for l in lines:
        if l.strip() == MARK_START.strip():
            skip = True
            continue
        if l.strip() == MARK_END.strip():
            skip = False
            continue
        if not skip:
            kept.append(l)

    ml = next((i for i, l in enumerate(kept) if re.match(r"^model_list\s*:", l)), None)
    block = render_block(cfg)
    if ml is None:
        kept = ["model_list:", *block, *kept]
    else:
        kept[ml + 1:ml + 1] = block
    result = "\n".join(kept)

    parsed = yaml.safe_load(result)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("model_list"), list):
        raise ValueError("rendered config has no model_list")
    groups = {m.get("model_name") for m in parsed["model_list"]}
    for tier in TIERS:
        if cfg["tiers"].get(tier) and f"tier:{tier}" not in groups:
            raise ValueError(f"tier:{tier} missing after render")
    return result


# --- secrets ------------------------------------------------------------------------------
def env_path() -> str:
    return os.environ.get("DNC_ENV_FILE", os.path.expanduser("~/dnc/.env"))


def set_env_var(key: str, value: str, path: str | None = None) -> str:
    """Upsert `key=value` into the env file at chmod 600.

    This is where a gateway key MUST live: the systemd unit loads it via EnvironmentFile,
    so exporting it in your shell is not enough to survive a restart. The value is never
    logged or returned anywhere."""
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
        raise ValueError(f"bad env var name: {key!r}")
    if "\n" in value or "\r" in value:
        raise ValueError("value must be a single line")
    p = path or env_path()
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    lines = []
    if os.path.exists(p):
        with open(p) as f:
            lines = f.read().splitlines()
    pat = re.compile(rf"^\s*(export\s+)?{re.escape(key)}\s*=")
    replaced = False
    for i, l in enumerate(lines):
        if pat.match(l):
            lines[i] = f"{key}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{key}={value}")
    tmp = f"{p}.tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return p


def has_env_var(key: str, path: str | None = None) -> bool:
    p = path or env_path()
    if not os.path.exists(p):
        return False
    pat = re.compile(rf"^\s*(export\s+)?{re.escape(key)}\s*=\s*\S")
    with open(p) as f:
        return any(pat.match(l) for l in f)


def mask(value: str) -> str:
    """For display only — never return a raw key over the API."""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]}"
