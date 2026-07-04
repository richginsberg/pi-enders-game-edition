"""Grok (grok-cli proxy) module.

Bridges Composer 2.5 / grok-build — served by grok-cli's private proxy
(cli-chat-proxy.grok.com) — into the OpenAI shape. The endpoint is gated to a legit
grok-cli client, so we REUSE grok-cli's own helper binaries for the dynamic headers
(client version, user agent) rather than forge them, and read the OAuth JWT from
~/.grok/auth.json fresh per request (so token refreshes done by `grok login` / the
grok-pi extension are picked up).

STATUS: skeleton pending live-endpoint validation. UNVERIFIED until probed on the
control-plane box where grok-cli is installed:
  - whether the endpoint speaks /chat/completions or the OpenAI Responses API
    (/responses) — grok-pi declares api "openai-responses". Override `_url()` /
    add request translation once confirmed.
  - the exact header set the proxy requires. Reusing the helper binaries is the
    robust bet; hardcoding client-version will break when Grok bumps it.
Not enabled in config.example.yaml until validated.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .passthrough import OpenAIPassthrough

DEFAULT_BASE = "https://cli-chat-proxy.grok.com/v1"


class GrokModule(OpenAIPassthrough):
    def __init__(
        self,
        name: str = "grok",
        base_url: str = DEFAULT_BASE,
        auth_json: str = "~/.grok/auth.json",
        auth_field: str = "grok-cli",
        model_ids: list[str] | None = None,
        model_map: dict[str, str] | None = None,
        client_version_cmd: list[str] | None = None,  # e.g. ["<bindir>/grok-client-version"]
        user_agent_cmd: list[str] | None = None,       # e.g. ["<bindir>/grok-user-agent"]
        **kw,
    ) -> None:
        super().__init__(
            name=name, base_url=base_url,
            model_ids=model_ids or ["grok-composer-2.5-fast", "grok-build"],
            model_map=model_map, **kw,
        )
        self.auth_json = Path(auth_json).expanduser()
        self.auth_field = auth_field
        self.client_version_cmd = client_version_cmd
        self.user_agent_cmd = user_agent_cmd

    def _token(self) -> str:
        # Re-read each call so grok-cli token refreshes are picked up.
        data = json.loads(self.auth_json.read_text())
        return data[self.auth_field]["key"]

    @staticmethod
    def _run(cmd: list[str] | None) -> str | None:
        if not cmd:
            return None
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None

    def headers(self, body: dict | None = None) -> dict[str, str]:
        h = {
            "content-type": "application/json",
            "authorization": f"Bearer {self._token()}",
            "x-xai-token-auth": "xai-grok-cli",
        }
        cv = self._run(self.client_version_cmd)
        ua = self._run(self.user_agent_cmd)
        if cv:
            h["x-grok-client-version"] = cv
        if ua:
            h["user-agent"] = ua
        if body and body.get("model"):
            h["x-grok-model-override"] = self.model_map.get(body["model"], body["model"])
        return h
