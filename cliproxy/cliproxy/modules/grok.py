"""Grok (grok-cli) module — EXPERIMENTAL, DISABLED.

STATUS (2026-07-04): reverse-engineering the grok-cli chat protocol was ATTEMPTED and
BLOCKED. The `grok` binary (xai-grok-shell, Rust) is interception-resistant:
  - base-URL env vars (GROK_CODE_BACKEND_URL etc.) do NOT redirect the chat proxy;
  - it IGNORES HTTP(S)_PROXY and connects directly to cli-chat-proxy.grok.com;
  - it almost certainly bundles its own CA roots, so mitmproxy can't decrypt.
So the exact wire format (path: /v1/responses vs /chat/completions vs /rest/app-chat;
the gated header VALUES: x-grok-client-version / x-grok-client-identifier /
x-grok-client-surface; the request/response body) could NOT be captured remotely.
Capturing would need root-level transparent proxying AND defeating cert pinning.

What IS known and implemented here (so the module is correct if the protocol is ever
captured): the real auth. `~/.grok/auth.json` is an OIDC token store keyed by
`<oidc_issuer>::<client_id>`, value carrying `key` (short-lived access JWT, ~6h),
`refresh_token`, `expires_at`, `oidc_issuer`, `oidc_client_id`. We read the entry fresh
per call and refresh via OIDC when expired.

RECOMMENDATION: for a stable Grok in the fleet, use an xAI API key (console.x.ai,
`xai-…`) with LiteLLM's native `xai/` provider — NOT this CLI-proxy bridge.

Not enabled in cliproxy.example.yaml.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .passthrough import OpenAIPassthrough

DEFAULT_BASE = "https://cli-chat-proxy.grok.com/v1"


class GrokModule(OpenAIPassthrough):
    def __init__(
        self,
        name: str = "grok",
        base_url: str = DEFAULT_BASE,
        auth_json: str = "~/.grok/auth.json",
        oidc_issuer: str | None = None,   # pick the auth.json entry by issuer; None = first with a key
        refresh: bool = True,
        model_ids: list[str] | None = None,
        model_map: dict[str, str] | None = None,
        gated_headers: dict[str, str] | None = None,  # x-grok-client-version/identifier/surface (UNKNOWN — capture blocked)
        **kw,
    ) -> None:
        super().__init__(
            name=name, base_url=base_url,
            model_ids=model_ids or ["grok-composer-2.5-fast", "grok-build"],
            model_map=model_map, **kw,
        )
        self.auth_json = Path(auth_json).expanduser()
        self.oidc_issuer = oidc_issuer
        self.refresh = refresh
        self.gated_headers = gated_headers or {}

    # -- OIDC auth.json handling (this part is verified against the real file) --
    def _entry(self) -> dict:
        data = json.loads(self.auth_json.read_text())
        for k, v in data.items():
            if not isinstance(v, dict) or "key" not in v:
                continue
            if self.oidc_issuer is None or v.get("oidc_issuer") == self.oidc_issuer:
                return v
        raise RuntimeError(f"no usable OIDC entry in {self.auth_json}")

    @staticmethod
    def _expired(entry: dict) -> bool:
        exp = entry.get("expires_at")
        if not exp:
            return False
        try:  # e.g. 2026-07-04T22:15:36.976525265Z — drop nanoseconds + Z, treat as UTC
            from datetime import datetime, timezone
            s = exp.split(".")[0].rstrip("Z")
            t = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
            return t.timestamp() <= time.time() + 60
        except ValueError:
            return False

    def _refresh_token(self, entry: dict) -> str:
        """Standard OIDC refresh: discover token_endpoint, exchange refresh_token."""
        import httpx

        issuer = entry["oidc_issuer"].rstrip("/")
        with httpx.Client(timeout=30) as c:
            conf = c.get(f"{issuer}/.well-known/openid-configuration").json()
            r = c.post(conf["token_endpoint"], data={
                "grant_type": "refresh_token",
                "refresh_token": entry["refresh_token"],
                "client_id": entry["oidc_client_id"],
            })
            r.raise_for_status()
            return r.json()["access_token"]

    def _token(self) -> str:
        entry = self._entry()
        if self.refresh and self._expired(entry):
            return self._refresh_token(entry)
        return entry["key"]

    # -- headers: auth is correct; the gated client signature is UNKNOWN --------
    def headers(self, body: dict | None = None) -> dict[str, str]:
        h = {
            "content-type": "application/json",
            "authorization": f"Bearer {self._token()}",
            "x-xai-token-auth": "xai-grok-cli",
            **self.gated_headers,  # x-grok-client-version/identifier/surface — MUST be supplied; capture was blocked
        }
        if body and body.get("model"):
            h["x-grok-model-override"] = self.model_map.get(body["model"], body["model"])
        return h
