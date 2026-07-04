"""cliproxy CLI: `dnc-cliproxy serve`."""

from __future__ import annotations

import argparse
import os


def main() -> None:
    ap = argparse.ArgumentParser(prog="dnc-cliproxy")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the OpenAI-compatible auth-bridge")
    s.add_argument("--config", default=os.environ.get("DNC_CLIPROXY_CONFIG", "cliproxy.yaml"))
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=7433)
    args = ap.parse_args()

    if args.cmd == "serve":
        os.environ["DNC_CLIPROXY_CONFIG"] = args.config
        import uvicorn

        uvicorn.run("cliproxy.api:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
