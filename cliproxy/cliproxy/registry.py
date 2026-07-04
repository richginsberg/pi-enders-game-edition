"""Build the enabled ProviderModules from config.

config (YAML) shape:
    modules:
      - type: passthrough        # or: grok
        name: deepinfra
        base_url: https://api.deepinfra.com/v1/openai
        api_key: ${DEEPINFRA_KEY}   # ${VAR} expands from env
        model_ids: [Qwen/Qwen3-Embedding-0.6B]
        extra_headers: {}
        model_map: {}
"""

from __future__ import annotations

import os
import re
from typing import Any

from .base import ProviderModule
from .modules.grok import GrokModule
from .modules.passthrough import OpenAIPassthrough

_TYPES = {"passthrough": OpenAIPassthrough, "grok": GrokModule}
_ENV = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand(v: Any) -> Any:
    """Expand ${VAR} from env in strings; recurse into dict/list."""
    if isinstance(v, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), ""), v)
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_expand(x) for x in v]
    return v


def build_modules(config: dict) -> list[ProviderModule]:
    mods: list[ProviderModule] = []
    for spec in config.get("modules", []):
        spec = _expand(dict(spec))
        mtype = spec.pop("type", "passthrough")
        cls = _TYPES.get(mtype)
        if cls is None:
            raise ValueError(f"unknown module type: {mtype}")
        mods.append(cls(**spec))
    return mods


def find_module(mods: list[ProviderModule], model: str) -> ProviderModule | None:
    return next((m for m in mods if m.owns(model)), None)
