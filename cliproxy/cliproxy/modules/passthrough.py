"""OpenAI-passthrough module — forwards to any OpenAI-compatible upstream.

The reference module and the workhorse: point it at any `/v1` base that speaks
OpenAI Chat Completions, with a bearer key and optional extra headers. Also the base
class for providers that are "OpenAI-compatible once you fix the headers/auth" — e.g.
the Grok module subclasses this and just overrides how the headers are produced.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from ..base import ProviderModule


class OpenAIPassthrough(ProviderModule):
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str | None = None,
        model_ids: list[str] | None = None,
        extra_headers: dict[str, str] | None = None,
        model_map: dict[str, str] | None = None,
        timeout: float = 300.0,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_ids = model_ids or []
        self.extra_headers = extra_headers or {}
        self.model_map = model_map or {}  # exposed id -> upstream id
        self.timeout = timeout

    # -- catalog ---------------------------------------------------------------
    def models(self) -> list[dict]:
        return [{"id": m, "object": "model", "owned_by": self.name} for m in self.model_ids]

    def owns(self, model: str) -> bool:
        return model in self.model_ids

    # -- request assembly (overridable; the Grok module overrides headers) -----
    def headers(self, body: dict | None = None) -> dict[str, str]:
        h = {"content-type": "application/json", **self.extra_headers}
        if self.api_key and self.api_key != "none":
            h.setdefault("authorization", f"Bearer {self.api_key}")
        return h

    def _upstream_body(self, body: dict) -> dict:
        out = dict(body)
        out["model"] = self.model_map.get(body.get("model", ""), body.get("model"))
        return out

    def _url(self) -> str:
        return f"{self.base_url}/chat/completions"

    # -- calls -----------------------------------------------------------------
    async def chat(self, body: dict) -> dict:
        import httpx  # lazy: pure request-assembly logic tests without the dep

        payload = self._upstream_body({**body, "stream": False})
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(self._url(), json=payload, headers=self.headers(payload))
            r.raise_for_status()
            return r.json()

    async def chat_stream(self, body: dict) -> AsyncIterator[bytes]:
        import httpx

        payload = self._upstream_body({**body, "stream": True})
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream("POST", self._url(), json=payload, headers=self.headers(payload)) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if line:  # re-frame each SSE line the upstream sent
                        yield f"{line}\n\n".encode()
