#!/usr/bin/env python3
"""Validate cross-host prefix-affinity routing (#5) + failover (#25) against the live gateway.

The deferred M1 test: with >=2 nodes in a squad, does the router's prefix-hash affinity
(a) keep a given prompt-prefix pinned to ONE node (so its KV/prompt cache stays warm),
and (b) distribute distinct prefixes across nodes? We detect which node served a prompt
by probing each node directly for a cache hit (the node that served it has the prefix cached).

Signals (both from the OpenAI usage block): prompt_tokens_details.cached_tokens.

Env: GATEWAY (http://host:4000/v1), MASTER_KEY, NODES ("id=base,id=base").
Usage: GATEWAY=... MASTER_KEY=... NODES="s3-node-01=http://192.168.1.106:8080/v1,..." python3 tools/affinity_ab.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

GATEWAY = os.environ["GATEWAY"].rstrip("/")
KEY = os.environ["MASTER_KEY"]
NODES = dict(kv.split("=", 1) for kv in os.environ["NODES"].split(","))


def chat(base: str, prompt: str, key: str | None = None, model: str = "tier:s3", max_tokens: int = 8) -> dict:
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": max_tokens, "temperature": 0.0}).encode()
    req = urllib.request.Request(f"{base}/chat/completions", data=body, method="POST")
    req.add_header("content-type", "application/json")
    if key:
        req.add_header("authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)


def cached(resp: dict) -> int:
    return (resp.get("usage", {}).get("prompt_tokens_details") or {}).get("cached_tokens", 0)


def which_node(prefix: str) -> str:
    """Probe each node directly (max_tokens=1); the one with the prefix warm in KV served it."""
    best, best_hit = "?", -1
    for nid, base in NODES.items():
        h = cached(chat(base, prefix, model="q", max_tokens=1))
        if h > best_hit:
            best, best_hit = nid, h
    return best


def main() -> None:
    # 3 distinct "sessions" — long shared prefix each (repo-context-like), unique per session
    sessions = {f"sess{i}": f"SESSION-{i} repository context. " + ("def helper(x): return x*{}. ".format(i) * 300)
                for i in range(3)}

    print("=== AFFINITY (#5): each session sent 3x via gateway — expect cold→warm (pinned to one node) ===")
    routed = {}
    for name, prefix in sessions.items():
        hits = []
        for _ in range(3):
            hits.append(cached(chat(GATEWAY, prefix, key=KEY)))
        node = which_node(prefix)
        routed[name] = node
        warm = "WARM (affinity holds)" if hits[-1] > 0 and hits[0] < hits[-1] else "no warm-up"
        print(f"  {name}: cached_tokens {hits} -> {warm} | routed to {node}")

    print(f"\n=== DISTRIBUTION: sessions across nodes -> {dict(routed)}")
    uniq = set(routed.values())
    print(f"  {len(uniq)} of {len(NODES)} nodes used {'(spread ✓)' if len(uniq) > 1 else '(all one node — check hash spread)'}")

    print("\n=== STICKINESS: re-send each session, confirm same node (still warm) ===")
    for name, prefix in sessions.items():
        h = cached(chat(GATEWAY, prefix, key=KEY))
        print(f"  {name}: cached_tokens={h} {'✓ same warm node' if h > 0 else '✗ moved (cold)'}")


if __name__ == "__main__":
    main()
