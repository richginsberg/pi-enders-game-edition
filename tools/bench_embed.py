#!/usr/bin/env python3
"""Benchmark an OpenAI-compatible /embeddings endpoint.

Decides whether to self-host Qwen3-Embedding on the control-plane CPU (i7-9700T),
route to a fleet GPU node, or fall back to a vendor (DeepInfra). Measures the two
latencies that actually matter for the RAG layer:

  - query embed  (short text, blocks prompt injection -> must be snappy)
  - chunk embed  (paragraph, written at milestone/session-end -> can be async)

Stdlib only (urllib) so it runs anywhere, including a bare control-plane box.

Stand up a local CPU server to test the 9700T fallback:
    llama-server -m Qwen3-Embedding-0.6B-Q8_0.gguf --embeddings -t 4 --port 8090
    ./bench_embed.py --base http://localhost:8090/v1 --model qwen3-embedding-0.6b

Or point at the fleet via LiteLLM, or at DeepInfra:
    ./bench_embed.py --base https://api.deepinfra.com/v1/openai \\
        --model Qwen/Qwen3-Embedding-0.6B --api-key "$DEEPINFRA_API_KEY"
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import urllib.request

# ~40-token query vs ~380-token chunk: the two real request shapes.
QUERY = "How does the router pick a squad for a high-complexity refactor task?"
CHUNK = (
    "Decision: the migration play brings up the standard container on a fresh port and only "
    "stops the adopted server after the replacement passes its health check. Rationale: a "
    "1-2.5GbE fleet rules out cross-host KV transfer, so cutover must be side-by-side rather "
    "than in-place. Constraint: adopted servers are monitor-only everywhere except stop_adopted, "
    "which runs solely mid-migration. Follow-up: the LiteLLM registration flip still needs the "
    "litellm_sync piece, which is not yet built. " * 3
).strip()


def embed(base: str, model: str, text: str, api_key: str | None) -> tuple[float, int]:
    """POST one embedding request; return (elapsed_seconds, prompt_tokens)."""
    body = json.dumps({"model": model, "input": text}).encode()
    req = urllib.request.Request(f"{base}/embeddings", data=body, method="POST")
    req.add_header("content-type", "application/json")
    if api_key:
        req.add_header("authorization", f"Bearer {api_key}")
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.loads(resp.read())
    elapsed = time.perf_counter() - t0
    tokens = payload.get("usage", {}).get("prompt_tokens", 0)
    return elapsed, tokens


def run(base: str, model: str, text: str, n: int, api_key: str | None) -> None:
    embed(base, model, text, api_key)  # warm-up (load weights / connect), not counted
    samples = [embed(base, model, text, api_key) for _ in range(n)]
    lat = sorted(s[0] for s in samples)
    toks = statistics.median(s[1] for s in samples) or len(text.split())
    p50 = lat[len(lat) // 2]
    p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))]
    tps = toks / p50 if p50 else 0
    label = "query" if text is QUERY else "chunk"
    print(f"  {label:5s} (~{int(toks):4d} tok)  p50={p50 * 1000:6.0f}ms  p95={p95 * 1000:6.0f}ms  ~{tps:5.0f} tok/s")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("DNC_EMBED_BASE", "http://localhost:8090/v1"))
    ap.add_argument("--model", default=os.environ.get("DNC_EMBED_MODEL", "qwen3-embedding-0.6b"))
    ap.add_argument("--api-key", default=os.environ.get("DNC_EMBED_KEY"))
    ap.add_argument("-n", type=int, default=20, help="samples per shape")
    args = ap.parse_args()

    print(f"endpoint: {args.base}  model: {args.model}  n={args.n}")
    run(args.base, args.model, QUERY, args.n, args.api_key)
    run(args.base, args.model, CHUNK, args.n, args.api_key)
    print("\ngo/no-go for self-host: query p50 < 500ms and chunk p50 < 2s (leave cores for the proxy).")


if __name__ == "__main__":
    main()
