#!/usr/bin/env python3
"""Compare two local coding models head-to-head on one OpenAI-compatible endpoint.

Built for the BC-250 S3 shootout: current Qwen3-Coder-30B-A3B (pruned) vs
Qwen3.6-35B-REAP (pruned). The node holds one model at a time, so:

  1. load model A, run:  ./model_bench.py --base http://<node>:8080/v1 --model <alias> --tag A
  2. swap to model B, run: ... --tag B
  3. compare:            ./model_bench.py --compare A B

Measures, per coding task: prefill tok/s + generation tok/s (from llama.cpp `timings`)
and — with --run-code — functional correctness (extracts the code block, appends a unit
test, runs it in a subprocess with a timeout). Results are saved to results/<tag>.json.

SECURITY: --run-code executes model-generated code locally. The built-in tasks are benign
algorithms, but run it only where that's acceptable (it uses a temp dir + timeout, not a
real sandbox). Default is OFF — speed only — unless you pass --run-code.

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

RESULTS = Path(os.environ.get("DNC_BENCH_RESULTS", "results"))

# (id, prompt, unit-test appended after the model's code). Keep tasks small + verifiable.
TASKS = [
    ("is_prime", "Write a Python function is_prime(n) returning True iff n is prime. Code only.",
     "assert is_prime(2) and is_prime(97) and not is_prime(1) and not is_prime(91)"),
    ("balanced", "Write Python is_balanced(s): True iff ()[]{} in s are balanced. Code only.",
     "assert is_balanced('([]{})') and not is_balanced('(]') and is_balanced('')"),
    ("binsearch", "Write Python binsearch(a, x): index of x in sorted list a, else -1. Code only.",
     "assert binsearch([1,3,5,7],5)==2 and binsearch([1,3,5,7],4)==-1"),
    ("rle", "Write Python rle(s): run-length encode 'aaabb'->'a3b2'. Code only.",
     "assert rle('aaabb')=='a3b2' and rle('')=='' and rle('x')=='x1'"),
    ("merge", "Write Python merge(a, b): merge two sorted lists into one sorted list. Code only.",
     "assert merge([1,3],[2,4])==[1,2,3,4] and merge([],[1])==[1]"),
    ("anagram", "Write Python is_anagram(a, b): True iff a and b are anagrams (ignore case/space). Code only.",
     "assert is_anagram('Listen','Silent') and not is_anagram('a','ab')"),
]


def chat(base, model, prompt, key=None, max_tokens=512, timeout=120):
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": max_tokens, "temperature": 0.0}).encode()
    req = urllib.request.Request(f"{base.rstrip('/')}/chat/completions", data=body, method="POST")
    req.add_header("content-type", "application/json")
    if key:
        req.add_header("authorization", f"Bearer {key}")
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    return d, time.perf_counter() - t0


def extract_code(text):
    m = re.search(r"```(?:python)?\s*(.+?)```", text or "", re.S)
    return m.group(1) if m else (text or "")


def run_check(code, test):
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "cand.py"
        f.write_text(code + "\n\n" + test + "\nprint('PASS')\n")
        try:
            p = subprocess.run([sys.executable, str(f)], capture_output=True, text=True, timeout=10, cwd=td)
            return p.returncode == 0 and "PASS" in p.stdout
        except (subprocess.SubprocessError, OSError):
            return False


def bench(args):
    RESULTS.mkdir(exist_ok=True)
    rows = []
    for tid, prompt, test in TASKS:
        try:
            d, wall = chat(args.base, args.model, prompt, args.key, args.max_tokens)
            msg = d.get("choices", [{}])[0].get("message", {})
            content = msg.get("content") or msg.get("reasoning_content", "")
            t = d.get("timings", {})
            ok = run_check(extract_code(content), test) if args.run_code else None
            rows.append({"task": tid, "prefill_tps": round(t.get("prompt_per_second", 0), 1),
                         "gen_tps": round(t.get("predicted_per_second", 0), 1),
                         "gen_tokens": t.get("predicted_n", 0), "wall_s": round(wall, 1), "pass": ok})
            print(f"  {tid:10s} gen={rows[-1]['gen_tps']:6.1f} tok/s prefill={rows[-1]['prefill_tps']:6.1f} "
                  f"{'PASS' if ok else 'FAIL' if ok is False else '-'}")
        except Exception as e:
            rows.append({"task": tid, "error": str(e)[:80]})
            print(f"  {tid:10s} ERROR {e}")
    gens = [r["gen_tps"] for r in rows if r.get("gen_tps")]
    passes = [r["pass"] for r in rows if r.get("pass") is not None]
    summary = {"model": args.model, "base": args.base,
               "avg_gen_tps": round(sum(gens) / len(gens), 1) if gens else 0,
               "pass_rate": f"{sum(passes)}/{len(passes)}" if passes else "n/a (use --run-code)",
               "tasks": rows}
    (RESULTS / f"{args.tag}.json").write_text(json.dumps(summary, indent=2))
    print(f"\n{args.tag}: avg gen {summary['avg_gen_tps']} tok/s | correctness {summary['pass_rate']}")


def compare(tags):
    print(f"{'metric':16s} " + " ".join(f"{t:>18s}" for t in tags))
    data = {t: json.loads((RESULTS / f"{t}.json").read_text()) for t in tags}
    for label, key in (("model", "model"), ("avg gen tok/s", "avg_gen_tps"), ("correctness", "pass_rate")):
        print(f"{label:16s} " + " ".join(f"{str(data[t][key]):>18.18s}" for t in tags))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080/v1")
    ap.add_argument("--model", default="model")
    ap.add_argument("--key")
    ap.add_argument("--tag", help="save results under this tag")
    ap.add_argument("--max-tokens", type=int, default=512)
    ap.add_argument("--run-code", action="store_true", help="execute generated code to check correctness")
    ap.add_argument("--compare", nargs="+", metavar="TAG", help="compare saved result tags")
    args = ap.parse_args()
    if args.compare:
        compare(args.compare)
    elif args.tag:
        print(f"benchmarking {args.model} @ {args.base}  (run_code={args.run_code})")
        bench(args)
    else:
        ap.error("pass --tag to benchmark, or --compare TAG TAG to compare")


if __name__ == "__main__":
    main()
