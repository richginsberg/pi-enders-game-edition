#!/usr/bin/env python3
"""M1 exit benchmark for the divide-and-conquer fleet gateway.

Measures the three M1 exit signals against a live LiteLLM gateway:
  1. tiered routing      — tier:auto sends work to the right squad by complexity
  2. prompt cache-hit    — repeated shared prefixes reuse a host's KV cache
  3. fan-out ceiling     — concurrent throughput/latency before it degrades

Stdlib only. Config via env:
  DNC_LITELLM_URL  (default http://127.0.0.1:4000)
  LITELLM_MASTER_KEY

CAVEAT — prefix-affinity A/B (route same-prefix -> same host, on vs off) requires
2+ hosts in ONE squad. With one host/squad it's a no-op (always the same host), so
that comparison is deferred until more rigs are online (task #5). What IS measurable
now: routing correctness, single-host prompt cache-hit, and the fan-out ceiling.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.environ.get("DNC_LITELLM_URL", "http://127.0.0.1:4000")
KEY = os.environ.get("LITELLM_MASTER_KEY", "")


def chat(model, messages, max_tokens=16, temperature=0.0, headers=None, timeout=90):
    body = json.dumps({"model": model, "messages": messages,
                       "max_tokens": max_tokens, "temperature": temperature}).encode()
    req = urllib.request.Request(f"{BASE}/v1/chat/completions", data=body, method="POST")
    req.add_header("content-type", "application/json")
    if KEY:
        req.add_header("authorization", f"Bearer {KEY}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    return d, time.perf_counter() - t0


def _backend(d):
    # local llama.cpp squads (s1 stepfun, s3 BC-250) emit `timings`; API squads (s0) don't
    return "local(llama.cpp)" if "timings" in d else "api(frontier)"


def routing(_):
    print("\n== 1. tiered routing (tier:auto by x-dnc-complexity) ==")
    for cx in ("low", "high", "max"):
        try:
            d, _ = chat("tier:auto", [{"role": "user", "content": "reply OK"}],
                        headers={"x-dnc-complexity": cx})
            print(f"  complexity={cx:4s} -> {_backend(d)}")
        except Exception as e:
            print(f"  complexity={cx:4s} -> ERROR {e}")


def cache_hit(args):
    print(f"\n== 2. prompt cache-hit on {args.model} (repeated shared prefix) ==")
    prefix = "You are a code reviewer. Repo context:\n" + \
             "\n".join(f"def util_{i}(x): return x*{i}+{i%7}" for i in range(300))

    def one(suffix):
        d, _ = chat(args.model,
                    [{"role": "user", "content": prefix + "\n\nQuestion: " + suffix}],
                    max_tokens=8)
        u = d.get("usage", {})
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        return u.get("prompt_tokens", 0), cached

    pt0, c0 = one("what do these share? (cold)")
    pt1, c1 = one("name util_5's constant. (warm, same prefix)")
    print(f"  cold : prompt={pt0} cached={c0} ({100*c0/max(pt0,1):.0f}%)")
    print(f"  warm : prompt={pt1} cached={c1} ({100*c1/max(pt1,1):.0f}%)  <- prefix cache reuse")


def fanout(args):
    print(f"\n== 3. fan-out ceiling on {args.model} ==")
    msg = [{"role": "user", "content": "Write one line of Python that reverses a string."}]
    for n in args.concurrency:
        def worker(_):
            try:
                d, lat = chat(args.model, msg, max_tokens=48)
                return lat, d.get("usage", {}).get("completion_tokens", 0), None
            except Exception as e:
                return None, 0, str(e)
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=n) as ex:
            res = list(ex.map(worker, range(n)))
        wall = time.perf_counter() - t0
        lats = sorted(r[0] for r in res if r[0] is not None)
        errs = [r[2] for r in res if r[2]]
        toks = sum(r[1] for r in res)
        p50 = lats[len(lats)//2] if lats else 0
        p95 = lats[min(len(lats)-1, int(len(lats)*0.95))] if lats else 0
        print(f"  N={n:2d}: ok={len(lats)}/{n} err={len(errs)} "
              f"p50={p50:.1f}s p95={p95:.1f}s agg={toks/wall:.1f} tok/s"
              + (f"  [{errs[0][:40]}]" if errs else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="tier:s3", help="explicit tier for cache/fanout tests")
    ap.add_argument("--concurrency", type=int, nargs="+", default=[1, 2, 4, 8])
    ap.add_argument("--only", choices=["routing", "cache", "fanout"], help="run one section")
    args = ap.parse_args()
    print(f"gateway: {BASE}")
    sections = {"routing": routing, "cache": cache_hit, "fanout": fanout}
    for name, fn in sections.items():
        if not args.only or args.only == name:
            fn(args)


if __name__ == "__main__":
    main()
