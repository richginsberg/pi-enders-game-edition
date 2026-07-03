"""Ops CLI for the context store: inspect partition, remember, recall.

    python -m context.cli partition
    python -m context.cli recall "how does the router pick a squad?"
    python -m context.cli remember decision "chose pgvector over sqlite-vec"
"""

from __future__ import annotations

import argparse

from .models import Kind
from .partition import detect_partition


def main() -> None:
    ap = argparse.ArgumentParser(prog="context")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("partition", help="print the detected partition key")
    sub.add_parser("serve", help="run the HTTP sidecar (POST /recall, /remember)")
    r = sub.add_parser("recall", help="retrieve relevant context for a query")
    r.add_argument("query")
    r.add_argument("-k", type=int, default=5)
    m = sub.add_parser("remember", help="store a fact")
    m.add_argument("kind", choices=[str(k) for k in Kind])
    m.add_argument("text")
    args = ap.parse_args()

    if args.cmd == "partition":
        print(detect_partition())
        return
    if args.cmd == "serve":
        from .api import serve
        serve()
        return

    from .service import ContextService  # imports psycopg; only needed for these paths

    svc = ContextService()
    if args.cmd == "recall":
        for hit in svc.recall(args.query, k=args.k):
            print(f"[{hit.score:.3f}] {hit.kind}: {hit.text}")
    elif args.cmd == "remember":
        n = svc.remember_facts([(Kind(args.kind), args.text)])
        print(f"added {n} item(s) to partition {svc.partition}")
    svc.close()


if __name__ == "__main__":
    main()
