"""dnc-cliproxy — a pluggable OpenAI-compatible auth-bridge.

Presents a standard OpenAI API (`/v1/chat/completions`, `/v1/models`) so LiteLLM (and
anything OpenAI-compatible) can consume it, and routes each requested model to a
`ProviderModule` that adapts some upstream's quirky auth/API into that standard shape.

Why: some providers gate access behind CLI-tool auth schemes (e.g. Grok's grok-cli
uses an OAuth JWT + `X-XAI-Token-Auth` header + client-version signature against a
private proxy). Rather than teach LiteLLM each quirk, a module here normalizes it, and
LiteLLM just registers a plain `openai/` model pointing at this proxy.

Layering:
- `base`      — ProviderModule interface
- `modules/`  — one file per provider (passthrough reference; grok added after probing)
- `registry`  — build the enabled modules from config
- `api`       — the FastAPI app (routes a model -> its owning module)
"""
