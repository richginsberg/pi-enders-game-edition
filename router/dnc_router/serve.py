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
from contextlib import asynccontextmanager


def main() -> None:
    ap = argparse.ArgumentParser(prog="dnc-serve")
    ap.add_argument("--config", default=os.environ.get("DNC_CONFIG", "litellm-config.yaml"))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=4000)
    args = ap.parse_args()

    # The proxy reads its config from this env var during its lifespan startup.
    os.environ["CONFIG_FILE_PATH"] = os.path.abspath(args.config)

    import litellm.proxy.proxy_server as ps

    from .strategy import DncRoutingStrategy

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
