#!/usr/bin/env python3
"""Generate the fixed benchmark prompt set -> tools/bench_prompts.json (deterministic).

3 sizes x 5 unique rounds. Rounds are DISTINCT content (no shared long prefix) so each
measures honest prefill, not KV-cache reuse. Large prompts embed a code module with seeded
bugs so the same set can later grade OUTPUT QUALITY across tiers, not just speed.

Run once; commit the JSON. Re-run only to intentionally regenerate the set.
"""
from __future__ import annotations

import json
import os

# --- small: short input, short output ---------------------------------------------
SMALL = [
    "Write a Python one-liner that reverses a string and explain it in one sentence.",
    "In one sentence each, give the time and space complexity of merge sort.",
    "Write a single bash command to find and delete files older than 30 days under /var/log.",
    "Convert the decimal number 2026 to binary and to hexadecimal. Show both.",
    "Explain the difference between a process and a thread in two sentences.",
]

# --- medium: a spec + small code, moderate output ---------------------------------
MEDIUM = [
    "Here is a function spec: `dedupe_stable(items)` returns a new list with duplicates "
    "removed while preserving first-seen order, works for any hashable elements, and does "
    "not mutate the input. Write the Python implementation, then 3 unit tests (including an "
    "empty list and a list with all duplicates), and note the time complexity.",
    "Design a REST endpoint `POST /users/{id}/follow` for a social app. Describe the request/"
    "response shape, the status codes for success, already-following, self-follow, and "
    "missing user, and the idempotency behaviour. Then sketch the FastAPI handler signature "
    "and the data-model changes needed.",
    "You have a list of (timestamp, temperature) readings sampled irregularly. Write a Python "
    "function `rolling_average(readings, window_seconds)` returning, for each reading, the "
    "average temperature over the trailing window. Include handling for out-of-order input "
    "and explain your approach in a short paragraph.",
    "Explain what a database index is, when a composite index on (a, b) helps a query and "
    "when it does not, and why an index can slow down writes. Then give one concrete SQL "
    "example of a query that benefits from a covering index.",
    "Write a Python `retry` decorator with exponential backoff and jitter that retries a "
    "function up to N times on a given exception type, waits base*2**attempt seconds (capped), "
    "and re-raises the last exception on exhaustion. Show usage and list two failure modes to "
    "watch for.",
]

# --- large: big context (a code module with bugs) + review, longer output ---------
# Deterministic module builders — each ~150-200 lines to push input toward ~3-4k tokens.
def _module(domain: str, funcs: list[tuple[str, str]]) -> str:
    header = f'"""{domain} — internal utility module (v1). NOT reviewed. Assume Python 3.11."""\n\n'
    body = "\n\n".join(
        f"def {name}({sig}):\n    " + impl.replace("\n", "\n    ") for name, (sig, impl) in
        ((n, s) for n, s in funcs)
    )
    return header + "import time\nimport math\nfrom collections import defaultdict, deque\n\n" + body


LARGE_MODULES = [
    ("an LRU cache + memoizer", [
        ("LRUCache.__init__", ("self, capacity", "self.capacity = capacity\nself.store = {}\nself.order = []")),
        ("LRUCache.get", ("self, key", "if key not in self.store:\n    return None\nself.order.remove(key)\nself.order.append(key)\nreturn self.store[key]")),
        ("LRUCache.put", ("self, key, value", "if key in self.store:\n    self.order.remove(key)\nelif len(self.store) >= self.capacity:\n    victim = self.order.pop()   # BUG: pops most-recent, should pop(0)\n    del self.store[victim]\nself.store[key] = value\nself.order.append(key)")),
        ("memoize", ("fn", "cache = {}\ndef wrap(*args):\n    if args in cache:\n        return cache[args]\n    r = fn(*args)\n    cache[args] = r\n    return r\nreturn wrap")),
    ]),
    ("a token-bucket rate limiter", [
        ("RateLimiter.__init__", ("self, rate, burst", "self.rate = rate\nself.burst = burst\nself.tokens = burst\nself.last = time.time()")),
        ("RateLimiter.allow", ("self, n=1", "now = time.time()\nself.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)\nself.last = now\nif self.tokens > n:   # BUG: should be >=\n    self.tokens -= n\n    return True\nreturn False")),
        ("RateLimiter.wait_time", ("self, n=1", "if self.tokens >= n:\n    return 0.0\nreturn (n - self.tokens) / self.rate")),
    ]),
    ("a CSV-ish line parser", [
        ("parse_line", ("line, sep=','", "out = []\ncur = ''\nin_q = False\nfor ch in line:\n    if ch == '\"':\n        in_q = not in_q\n    elif ch == sep and not in_q:\n        out.append(cur)\n        cur = ''\n    else:\n        cur += ch\nout.append(cur)\nreturn out   # BUG: does not unescape doubled quotes")),
        ("parse_rows", ("text, sep=','", "rows = []\nfor line in text.splitlines():\n    if not line.strip():\n        continue\n    rows.append(parse_line(line, sep))\nreturn rows")),
        ("to_dicts", ("rows", "header = rows[0]\nreturn [dict(zip(header, r)) for r in rows[1:]]   # BUG: no length check on r")),
    ]),
    ("a small graph library", [
        ("Graph.__init__", ("self", "self.adj = defaultdict(list)")),
        ("Graph.add_edge", ("self, u, v", "self.adj[u].append(v)\nself.adj[v].append(u)")),
        ("Graph.bfs", ("self, start", "seen = set([start])\nq = deque([start])\norder = []\nwhile q:\n    node = q.popleft()\n    order.append(node)\n    for nb in self.adj[node]:\n        if nb not in seen:\n            seen.add(nb)\n            q.append(nb)\nreturn order")),
        ("Graph.shortest_path", ("self, a, b", "prev = {a: None}\nq = deque([a])\nwhile q:\n    node = q.popleft()\n    if node == b:\n        break\n    for nb in self.adj[node]:\n        if nb not in prev:\n            prev[nb] = node\n            q.append(nb)\npath = []\ncur = b\nwhile cur is not None:\n    path.append(cur)\n    cur = prev[cur]   # BUG: KeyError if b unreachable\nreturn list(reversed(path))")),
    ]),
    ("a retry + circuit breaker", [
        ("CircuitBreaker.__init__", ("self, threshold, cooldown", "self.threshold = threshold\nself.cooldown = cooldown\nself.failures = 0\nself.opened_at = None")),
        ("CircuitBreaker.call", ("self, fn, *a", "if self.opened_at and time.time() - self.opened_at < self.cooldown:\n    raise RuntimeError('open')\ntry:\n    r = fn(*a)\n    self.failures = 0\n    return r\nexcept Exception:\n    self.failures += 1\n    if self.failures > self.threshold:\n        self.opened_at = time.time()\n    raise")),
        ("backoff_seconds", ("attempt, base=0.5, cap=30", "return min(cap, base * 2 ** attempt)   # BUG: no jitter -> thundering herd")),
    ]),
]


def _pad_helpers(seed: int, target_chars: int) -> str:
    """Deterministic filler functions (plausible code) to grow a module to a size target."""
    ops = ["x + y", "x * y - 1", "(x ^ y) & 0xff", "max(x, y) % 7", "abs(x - y) + 1",
           "(x << 1) | (y >> 1)", "x if x > y else y", "pow(x, 2) - y", "min(x, y) * 3"]
    out, i = [], 0
    while sum(len(s) for s in out) < target_chars:
        op = ops[(seed + i) % len(ops)]
        out.append(
            f"\n\ndef helper_{seed}_{i}(x, y):\n"
            f'    """Combines two integers; part of the {seed}-series arithmetic helpers."""\n'
            f"    acc = 0\n"
            f"    for k in range({3 + (i % 5)}):\n"
            f"        acc += ({op}) + k\n"
            f"    return acc\n"
        )
        i += 1
    return "".join(out)


def build() -> dict:
    small = [{"user": p} for p in SMALL]
    medium = [{"user": p} for p in MEDIUM]
    large = []
    for idx, (domain, funcs) in enumerate(LARGE_MODULES):
        code = _module(domain, funcs)
        code += _pad_helpers(idx, 12000)  # -> module ~3.5k tokens of real-ish code
        # pad toward ~3.5k tokens with a second copy under a "helpers" banner (keeps it coherent)
        prompt = (
            "You are reviewing an unreviewed internal Python module. Read it carefully and "
            "produce: (1) a numbered list of every correctness bug with the exact line/behaviour "
            "and a one-line fix, (2) any missing edge-case handling, (3) a corrected version of "
            "the single most important function. Be precise and do not invent issues.\n\n"
            "```python\n" + code + "\n```\n"
        )
        large.append({"user": prompt})
    return {
        "_meta": "DnC node/tier benchmark set. 3 sizes x 5 unique rounds. temp=0. See tools/bench_nodes.py.",
        "small": {"max_tokens": 64, "rounds": small},
        "medium": {"max_tokens": 256, "rounds": medium},
        "large": {"max_tokens": 512, "rounds": large},
    }


def main() -> None:
    out = os.path.join(os.path.dirname(__file__), "bench_prompts.json")
    data = build()
    with open(out, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    for size in ("small", "medium", "large"):
        chars = [len(r["user"]) for r in data[size]["rounds"]]
        print(f"{size}: {len(chars)} rounds, ~{sum(chars)//len(chars)} chars (~{sum(chars)//len(chars)//4} tok) avg, "
              f"max_tokens={data[size]['max_tokens']}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
