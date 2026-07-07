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
