"""Run the LiteLLM proxy with the DnC custom routing strategy attached.

The proxy YAML has no key to register a custom routing strategy (it's a Router/SDK
feature). So we import the proxy's FastAPI app and WRAP its lifespan: let the proxy
build its Router during startup, then rebind routing to DncRoutingStrategy via
Router.set_custom_routing_strategy(). This is what makes `tier:auto` (complexity
tiers + prefix-hash affinity) work. Plain `litellm --config` still serves the
explicit tiers (tier:s0..s3) with default routing.

    python -m dnc_router.serve --config ~/dnc/litellm-config.yaml --host 0.0.0.0 --port 4000

Secrets (master key, provider keys) come from the environment exactly as the normal
proxy — source your .env first.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from contextlib import asynccontextmanager


def _ensure_prisma_ready() -> None:
    """Replicate the DB setup the stock `litellm` CLI does before serving.

    The `litellm --config` CLI runs `prisma generate` (build the client) and `prisma db push`
    (create the LiteLLM_* tables) before starting. Our direct-app launcher calls the proxy
    lifespan straight, so it skips both — without this, a DB-backed proxy dies at startup with
    "Client hasn't been generated" and then "relation LiteLLM_SpendLogs does not exist".

    Idempotent and best-effort: no-op unless DATABASE_URL is set; generate only if the client
    is missing; then sync the schema (a no-op when already in sync). Prisma reads DATABASE_URL
    from the env, so source your .env before launching.
    """
    if not os.environ.get("DATABASE_URL"):
        return
    import litellm

    schema = os.path.join(os.path.dirname(litellm.__file__), "proxy", "schema.prisma")
    if not os.path.exists(schema):
        print(f"[dnc] WARNING: litellm prisma schema not found at {schema}; skipping DB setup")
        return

    try:
        from prisma import Prisma  # noqa: F401 — raises if the client isn't generated yet
    except Exception:
        print("[dnc] prisma client not generated — running `prisma generate`")
        subprocess.run([sys.executable, "-m", "prisma", "generate", "--schema", schema], check=True)

    print("[dnc] syncing DB schema (`prisma db push`)")
    subprocess.run(
        [sys.executable, "-m", "prisma", "db", "push", "--schema", schema, "--skip-generate"],
        check=True,
    )


def main() -> None:
    ap = argparse.ArgumentParser(prog="dnc-serve")
    ap.add_argument("--config", default=os.environ.get("DNC_CONFIG", "litellm-config.yaml"))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=4000)
    args = ap.parse_args()

    # The proxy reads its config from this env var during its lifespan startup.
    os.environ["CONFIG_FILE_PATH"] = os.path.abspath(args.config)

    # Match the litellm CLI's DB bootstrap (generate + db push) before the proxy starts;
    # our direct-app launcher would otherwise skip it. No-op without DATABASE_URL.
    _ensure_prisma_ready()

    import litellm.proxy.proxy_server as ps

    from .strategy import DncRoutingStrategy, _as_dict, squad_for_deployment_id

    # Echo the RESOLVED squad on every response. The proxy already returns
    # x-litellm-model-id (the deployment that served); we map it back to model_info.dnc_squad
    # and stamp x-dnc-squad so a tier:auto caller can see which tier actually answered
    # (the request-side x-dnc-tier/-complexity hint only says what was asked for). A plain
    # response middleware keeps this independent of litellm's internal header plumbing, and
    # works for streaming too (headers are set before the body streams). Best-effort: any
    # lookup failure just omits the header.
    from starlette.middleware.base import BaseHTTPMiddleware

    async def _stamp_squad(request, call_next):
        response = await call_next(request)
        try:
            model_id = response.headers.get("x-litellm-model-id")
            if model_id and ps.llm_router is not None:
                members = [_as_dict(d) for d in getattr(ps.llm_router, "model_list", []) or []]
                squad = squad_for_deployment_id(members, model_id)
                if squad:
                    response.headers["x-dnc-squad"] = squad
        except Exception:
            pass
        return response

    ps.app.add_middleware(BaseHTTPMiddleware, dispatch=_stamp_squad)

    # --- usage + cost capture -------------------------------------------------------
    # The Pi extension cannot compute this itself: its after_provider_response hook carries
    # only {status, headers}, and no extension event exposes token counts. LiteLLM's own
    # accounting is also unavailable in practice (response-cost header reads 0.0 with no
    # prices registered, and /spend/logs is empty while the custom strategy is attached —
    # task #27). The usage block IS in the body we're already wrapping, so capture it here
    # and expose a rollup at /dnc/usage for the extension to poll.
    from . import usage as U

    def _session_of(request) -> str:
        return (request.headers.get("x-dnc-session")
                or request.headers.get("x-session-id") or "main")

    def _model_of(model_id: str | None, response) -> str | None:
        """The UNDERLYING model string (e.g. openai/anthropic/claude-opus-5), not the model
        group ("tier:s0"). Pricing keys off the real model name, so returning the group here
        would silently make every table lookup miss and report everything as unpriced."""
        try:
            for d in (getattr(ps.llm_router, "model_list", []) or []):
                dd = _as_dict(d)
                if dd.get("model_info", {}).get("id") == model_id:
                    return dd.get("litellm_params", {}).get("model") or dd.get("model_name")
        except Exception:
            pass
        return response.headers.get("x-litellm-model-group")

    def _prices_for(model_id: str | None) -> tuple[float | None, float | None]:
        """Per-token prices the operator registered in model_info, if any."""
        try:
            for d in (getattr(ps.llm_router, "model_list", []) or []):
                dd = _as_dict(d)
                if dd.get("model_info", {}).get("id") == model_id:
                    mi = dd["model_info"]
                    return mi.get("input_cost_per_token"), mi.get("output_cost_per_token")
        except Exception:
            pass
        return None, None

    def _billing_for(model_id: str | None) -> str | None:
        try:
            for d in (getattr(ps.llm_router, "model_list", []) or []):
                dd = _as_dict(d)
                if dd.get("model_info", {}).get("id") == model_id:
                    return dd["model_info"].get("dnc_billing")
        except Exception:
            pass
        return None

    async def _capture_usage(request, call_next):
        response = await call_next(request)
        if not request.url.path.endswith(("/chat/completions", "/completions", "/responses")):
            return response
        try:
            headers = {k.lower(): v for k, v in response.headers.items()}
            model_id = headers.get("x-litellm-model-id")
            session = _session_of(request)
            model = _model_of(model_id, response)
            pin, pout = _prices_for(model_id)
            billing = _billing_for(model_id)

            def _record(u):
                if u is None:
                    return
                U.LEDGER.add(U.record_from(
                    session=session, headers=headers, usage=u, model=model,
                    declared_billing=billing, price_in=pin, price_out=pout))

            body_iter = getattr(response, "body_iterator", None)
            if body_iter is None:                      # buffered JSON response
                return response
            # Streaming: tee the frames through, then parse usage from the accumulated text.
            # We must not buffer the whole stream before forwarding it, or we'd destroy
            # time-to-first-token — so yield each chunk immediately and accumulate alongside.
            chunks: list[bytes] = []

            async def _tee():
                try:
                    async for chunk in body_iter:
                        chunks.append(chunk)
                        yield chunk
                finally:
                    try:
                        text = b"".join(chunks).decode("utf-8", "replace")
                        _record(U.usage_from_sse(text) or U.parse_usage(_maybe_json(text)))
                    except Exception:
                        pass

            response.body_iterator = _tee()
        except Exception:
            pass
        return response

    def _maybe_json(text: str):
        try:
            return __import__("json").loads(text)
        except Exception:
            return None

    ps.app.add_middleware(BaseHTTPMiddleware, dispatch=_capture_usage)

    @ps.app.get("/dnc/usage")
    async def _dnc_usage(session: str | None = None, detail: bool = False):
        """Live per-session usage rollup: tokens, cache hits, estimated spend, and the
        subscription / per-token / per-hour / local mix. Consumed by the Pi status bar."""
        out = {"session": session or "all", **U.LEDGER.rollup(session)}
        if detail:
            out["calls"] = [U.as_dict(r) for r in U.LEDGER.records(session)[-100:]]
        return out

    @ps.app.post("/dnc/usage/reset")
    async def _dnc_usage_reset():
        U.LEDGER.clear()
        return {"ok": True}

    proxy_lifespan = ps.proxy_startup_event  # the proxy's own @asynccontextmanager

    @asynccontextmanager
    async def dnc_lifespan(app):
        async with proxy_lifespan(app):  # builds ps.llm_router from the config
            if ps.llm_router is not None:
                ps.llm_router.set_custom_routing_strategy(DncRoutingStrategy(router=ps.llm_router))
                print("[dnc] custom routing strategy attached (tier:auto affinity active)")
            else:
                print("[dnc] WARNING: llm_router is None — custom strategy NOT attached")
            yield

    # Swap the app's lifespan for our wrapper before serving.
    ps.app.router.lifespan_context = dnc_lifespan

    import uvicorn

    uvicorn.run(ps.app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
