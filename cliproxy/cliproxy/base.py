"""ProviderModule interface — the contract every provider adapter implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator


class ProviderModule(ABC):
    """Adapts one upstream provider to the OpenAI Chat Completions shape.

    A module owns a set of model ids. The API layer aggregates every module's
    `models()` for /v1/models, and dispatches each /v1/chat/completions to the
    module whose `owns(model)` returns True.
    """

    name: str

    @abstractmethod
    def models(self) -> list[dict]:
        """OpenAI /v1/models entries this module serves: [{"id","object","owned_by"}]."""

    @abstractmethod
    def owns(self, model: str) -> bool:
        """True if this module handles the given model id."""

    @abstractmethod
    async def chat(self, body: dict) -> dict:
        """Non-streaming chat completion. `body` is an OpenAI request; return an
        OpenAI ChatCompletion dict."""

    @abstractmethod
    def chat_stream(self, body: dict) -> AsyncIterator[bytes]:
        """Streaming chat completion. Yield raw SSE frames (`b"data: {...}\\n\\n"`),
        ending with `b"data: [DONE]\\n\\n"`, in OpenAI streaming format."""
