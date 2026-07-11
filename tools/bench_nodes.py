#!/usr/bin/env python3
"""Node/tier speed + quality shoot-out on a fixed prompt set (tools/bench_prompts.json).

Sends each (size x round) prompt to every target, sequentially (single-slot nodes — no
concurrency, so timings are clean), interleaving targets within a round to share drift.
Records llama.cpp server-side `timings` (authoritative) + client wall time + the output text.

  # targets via --nodes or $DNC_BENCH_NODES (keep local IPs out of the repo). Examples:
  DNC_BENCH_NODES="nA=http://<fedora-node>:8080,nB=http://<ubuntu-node>:8080" python3 tools/bench_nodes.py
  # cross-tier through the gateway:
  python3 tools/bench_nodes.py --nodes "s3=http://<gateway>:4000" --model tier:s3 --key "$LITELLM_MASTER_KEY"

Raw results -> tools/bench_results/<ts>.jsonl (output text kept, for later quality grading).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import time
import urllib.request

HERE = os.path.dirname(__file__)


def post(base: str, model: str, content: str, max_tokens: int, key: str | None) -> dict:
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": content}],
                       "max_tokens": max_tokens, "temperature": 0.0}).encode()
    req = urllib.request.Request(f"{base.rstrip('/')}/v1/chat/completions", data=body, method="POST")
    req.add_header("content-type", "application/json")
    if key:
        req.add_header("authorization", f"Bearer {key}")
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.load(r)
    wall = time.monotonic() - t0
    tm = d.get("timings", {}) or {}
    msg = (d.get("choices") or [{}])[0].get("message", {})
    text = msg.get("content") or msg.get("reasoning_content") or ""
    return {
        "prompt_tokens": tm.get("prompt_n", (d.get("usage") or {}).get("prompt_tokens", 0)),
        "prefill_tps": round(tm.get("prompt_per_second", 0.0), 2),
        "gen_tokens": tm.get("predicted_n", (d.get("usage") or {}).get("completion_tokens", 0)),
        "gen_tps": round(tm.get("predicted_per_second", 0.0), 2),
        "wall_s": round(wall, 2),
        "output": text,
    }


def mean_sd(xs: list[float]) -> tuple[float, float]:
    xs = [x for x in xs if x]
    if not xs:
        return 0.0, 0.0
    return (round(st.mean(xs), 1), round(st.pstdev(xs), 1) if len(xs) > 1 else 0.0)


def main() -> int:
    ap = argparse.ArgumentParser()
    # name=url,name=url — pass at runtime or via $DNC_BENCH_NODES (keep local IPs out of the repo).
    ap.add_argument("--nodes", default=os.environ.get("DNC_BENCH_NODES", ""))
    ap.add_argument("--prompts", default=os.path.join(HERE, "bench_prompts.json"))
    ap.add_argument("--model", default="qwen3.6-35b-reap")
    ap.add_argument("--key", default=None)
    ap.add_argument("--sizes", default="small,medium,large")
    args = ap.parse_args()

    if not args.nodes.strip():
        raise SystemExit('set targets: --nodes "name=url,name=url" or $DNC_BENCH_NODES')
    nodes = dict(kv.split("=", 1) for kv in args.nodes.split(","))
    prompts = json.load(open(args.prompts))
    sizes = [s for s in args.sizes.split(",") if s in prompts]

    outdir = os.path.join(HERE, "bench_results")
    os.makedirs(outdir, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    raw = open(os.path.join(outdir, f"{stamp}.jsonl"), "w")

    print(f"=== shoot-out: {', '.join(nodes)} | model={args.model} | {stamp} ===")
    print("warming up each node (excluded from timing)…")
    for base in nodes.values():
        try:
            post(base, args.model, "warm up", 8, args.key)
        except Exception as e:  # noqa: BLE001
            print(f"  ! warmup failed for {base}: {e}")

    # results[size][node] = list of per-round dicts
    results: dict[str, dict[str, list[dict]]] = {s: {n: [] for n in nodes} for s in sizes}
    outputs: dict[tuple[str, int, str], str] = {}
    for size in sizes:
        mx = prompts[size]["max_tokens"]
        rounds = prompts[size]["rounds"]
        for i, r in enumerate(rounds):
            for name, base in nodes.items():  # interleave nodes within a round
                try:
                    res = post(base, args.model, r["user"], mx, args.key)
                except Exception as e:  # noqa: BLE001
                    print(f"  ! {size} round{i} {name}: {e}")
                    continue
                results[size][name].append(res)
                outputs[(size, i, name)] = res["output"]
                raw.write(json.dumps({"stamp": stamp, "size": size, "round": i, "node": name,
                                      **{k: v for k, v in res.items() if k != "output"},
                                      "output_len": len(res["output"])}) + "\n")
                print(f"  {size:6s} r{i} {name:5s}: in={res['prompt_tokens']:>4} "
                      f"prefill={res['prefill_tps']:>7.1f} t/s  gen={res['gen_tps']:>5.1f} t/s  wall={res['wall_s']}s")
    raw.close()

    print("\n=== SPEED (server-side, mean±sd over rounds) ===")
    print(f"{'size':7s}{'node':6s}{'in_tok':>7s}{'prefill t/s':>16s}{'gen t/s':>14s}{'wall s':>9s}")
    for size in sizes:
        for name in nodes:
            rs = results[size][name]
            if not rs:
                continue
            pfm, pfs = mean_sd([x["prefill_tps"] for x in rs])
            gm, gs = mean_sd([x["gen_tps"] for x in rs])
            wm, _ = mean_sd([x["wall_s"] for x in rs])
            intok = round(st.mean([x["prompt_tokens"] for x in rs]))
            print(f"{size:7s}{name:6s}{intok:>7d}{f'{pfm}±{pfs}':>16s}{f'{gm}±{gs}':>14s}{wm:>9.1f}")

    if len(nodes) == 2:
        a, b = list(nodes)
        print(f"\n=== HEAD-TO-HEAD ({b} vs {a}) ===")
        for size in sizes:
            ga = mean_sd([x["gen_tps"] for x in results[size][a]])[0]
            gb = mean_sd([x["gen_tps"] for x in results[size][b]])[0]
            pa = mean_sd([x["prefill_tps"] for x in results[size][a]])[0]
            pb = mean_sd([x["prefill_tps"] for x in results[size][b]])[0]
            dg = f"{(gb-ga)/ga*100:+.1f}%" if ga else "n/a"
            dp = f"{(pb-pa)/pa*100:+.1f}%" if pa else "n/a"
            print(f"  {size:6s} gen: {a} {ga:>5.1f} vs {b} {gb:>5.1f} t/s ({dg})  |  "
                  f"prefill: {a} {pa:>7.1f} vs {b} {pb:>7.1f} t/s ({dp})")
        # determinism / quality: identical output at temp=0?
        print("\n=== OUTPUT MATCH (temp=0; identical text between nodes?) ===")
        for size in sizes:
            n = len(prompts[size]["rounds"])
            same = sum(1 for i in range(n) if outputs.get((size, i, a)) == outputs.get((size, i, b))
                       and outputs.get((size, i, a)))
            print(f"  {size:6s}: {same}/{n} rounds byte-identical")
    print(f"\nraw -> tools/bench_results/{stamp}.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
