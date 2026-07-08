#!/usr/bin/env python3
"""Drive an S0-orchestrate → S3-fan-out pattern through the gateway and report the per-node
spread across the tier:s3 squad. The repeatable regression companion to the interactive Pi
test (see the /fleet fan-out prompt).

What it exercises:
  1. ORCHESTRATE (optional): one tier:auto request with x-dnc-complexity=max — should resolve
     to S0 (the frontier planner turn). Confirms complexity routing still escalates.
  2. FAN-OUT: N independent, distinctly-worded requests fired CONCURRENTLY at tier:s3. The
     router spreads them by prefix-hash affinity; because S3 is single-slot (--parallel 1),
     concurrent requests to the same preferred node spill to idle siblings. With N > nodes we
     expect most/all nodes to light up.

Detection is header-based (no node probing needed): the gateway stamps
  x-litellm-model-api-base  -> the node that served
  x-dnc-squad               -> the resolved squad (added by the DnC router middleware)

Env:
  GATEWAY      base URL incl. /v1   (e.g. http://192.168.1.64:4000/v1)
  MASTER_KEY   LiteLLM master key
  N            fan-out count               (default 8)
  MODEL        fan-out model group         (default tier:s3)
  MAX_TOKENS   per fan-out request         (default 32)
  SKIP_ORCH    set to skip the S0 phase    (default off)

Usage:
  GATEWAY=http://192.168.1.64:4000/v1 MASTER_KEY=$LITELLM_MASTER_KEY python3 tools/faninout_ab.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

GATEWAY = os.environ["GATEWAY"].rstrip("/")
KEY = os.environ["MASTER_KEY"]
N = int(os.environ.get("N", "8"))
MODEL = os.environ.get("MODEL", "tier:s3")
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "32"))
SKIP_ORCH = bool(os.environ.get("SKIP_ORCH"))

# Independent, distinctly-worded subtasks — distinct prefixes so affinity hashes them apart.
# (Mirrors the interactive Pi prompt; each would be one spawned subagent.)
TASKS = [
    "Write FizzBuzz for 1 to 100 in Python.",
    "Implement a sieve of Eratosthenes in JavaScript.",
    "Write an iterative Fibonacci function in Rust.",
    "Implement bubble sort in Go.",
    "Write a recursive factorial in Ruby.",
    "Implement Euclid's GCD in C.",
    "Write a palindrome checker in Java.",
    "Convert an integer to a Roman numeral in TypeScript.",
    "Implement binary search in Kotlin.",
    "Write a quicksort in Swift.",
    "Implement a stack using two queues in C#.",
    "Write a debounce function in plain JavaScript.",
]


def chat(prompt: str, model: str, complexity: str, max_tokens: int) -> dict:
    """POST one completion; return {status, squad, node, model_id} from response headers."""
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": max_tokens, "temperature": 0.0}).encode()
    req = urllib.request.Request(f"{GATEWAY}/chat/completions", data=body, method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("authorization", f"Bearer {KEY}")
    req.add_header("x-dnc-complexity", complexity)  # tier:auto reads this; tier:sN ignores it
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            h = {k.lower(): v for k, v in r.getheaders()}
            r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        h = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
        status = e.code
    except Exception as e:  # noqa: BLE001 — surface transport failures as a row, don't abort the sweep
        return {"status": f"ERR {type(e).__name__}", "squad": "?", "node": "?", "model_id": "?"}
    return {"status": status, "squad": h.get("x-dnc-squad", "?"),
            "node": (h.get("x-litellm-model-api-base", "?") or "?").replace("http://", "").replace("/v1", ""),
            "model_id": h.get("x-litellm-model-id", "?")}


def main() -> int:
    if not SKIP_ORCH:
        print("=== PHASE 1 — ORCHESTRATE: tier:auto @ complexity=max (expect S0) ===")
        o = chat("Plan (do not implement) how you would split a repo audit into 8 parallel tasks.",
                 "tier:auto", "max", 64)
        if o["squad"] == "s0" and str(o["status"]) == "200":
            verdict = "✓ escalated to S0"
        elif str(o["status"]) in ("401", "403"):
            verdict = "✗ reached gateway but S0 upstream rejected auth — check the S0 provider key"
        else:
            verdict = "✗ did NOT resolve to S0"
        print(f"  status={o['status']} squad={o['squad']} node={o['node']} {verdict}")

    print(f"\n=== PHASE 2 — FAN-OUT: {N} concurrent {MODEL} requests (expect spread across S3) ===")
    tasks = [TASKS[i % len(TASKS)] + f" (variant {i})" for i in range(N)]
    with ThreadPoolExecutor(max_workers=N) as ex:
        results = list(ex.map(lambda p: chat(p, MODEL, "low", MAX_TOKENS), tasks))

    for i, r in enumerate(results):
        print(f"  task{i:<2} status={r['status']} squad={r['squad']} node={r['node']}")

    served = [r["node"] for r in results if str(r["status"]) == "200" and r["node"] != "?"]
    hist = Counter(served)
    print(f"\n=== DISTRIBUTION across {len(hist)} node(s) ===")
    for node, c in sorted(hist.items()):
        print(f"  {node:<24} {c:>2}  {'█' * c}")

    errs = [r for r in results if str(r["status"]) != "200"]
    if errs:
        print(f"\n  {len(errs)} non-200 response(s) — check node health / gateway log")
    uniq = len(hist)
    print(f"\nRESULT: {len(served)}/{N} served, spread across {uniq} node(s) "
          f"{'✓ good fan-out' if uniq >= 3 else '⚠ narrow spread — check affinity/spill or node health'}")
    return 0 if served and not errs else 1


if __name__ == "__main__":
    sys.exit(main())
