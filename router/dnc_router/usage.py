"""Per-call usage + cost accounting for the gateway.

Why this lives in the router rather than the Pi extension: **the extension cannot see it.**
Pi's `after_provider_response` hook carries only `{status, headers}`, and no extension event
exposes token counts. Meanwhile LiteLLM's own accounting is unavailable to us in practice —
verified on a live gateway, `x-litellm-response-cost-original` is `0.0` (no per-token prices
registered) and `/spend/logs` returns `[]` (the custom routing strategy bypasses spend
logging — task #27).

The usage block *is* right here in the response the middleware already wraps, so we capture
it, price it, and expose a rollup the extension can poll:

    GET /dnc/usage?session=<id>   -> per-tier / per-billing-class totals

Everything in this module is pure and unit-tested except the two I/O shims at the bottom.

**Cost is ESTIMATED unless the operator registers real prices.** Precedence, best first:
  1. LiteLLM's computed cost (set `input_cost_per_token`/`output_cost_per_token` in model_info)
  2. our built-in default table, keyed by model string
  3. unknown -> recorded as None and reported as such. We never invent a number.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field

# --- billing classes -----------------------------------------------------------------
SUBSCRIPTION = "subscription"   # flat-rate plan behind a CLI/auth bridge; capped by session limits
PER_TOKEN = "per_token"         # hosted API billed per token
PER_HOUR = "per_hour"           # rented GPU billed by wall-clock
LOCAL = "local"                 # hardware you own; marginal cost is electricity
BILLING_CLASSES = (SUBSCRIPTION, PER_TOKEN, PER_HOUR, LOCAL)

_PRIVATE = re.compile(r"^https?://(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|localhost|\[?::1)")


def classify_billing(api_base: str | None, declared: str | None = None) -> str:
    """How is this deployment paid for? An explicit `model_info.dnc_billing` always wins —
    only the operator knows whether an endpoint is behind a subscription. We infer only the
    unambiguous case: a private-network address is hardware you own."""
    if declared in BILLING_CLASSES:
        return declared
    if api_base and _PRIVATE.match(api_base):
        return LOCAL
    return PER_TOKEN


# --- prices ---------------------------------------------------------------------------
#: Fallback $/1M tokens (input, output, cached-input) for the models we ship as defaults.
#: Verified 2026-07-25; THEY DRIFT. Registering input_cost_per_token in the LiteLLM config
#: overrides these and is strongly preferred.
DEFAULT_PRICES_PER_M: dict[str, tuple[float, float, float]] = {
    "claude-fable-5":      (10.00, 50.00, 1.00),
    "claude-opus-5":       (5.00, 25.00, 0.50),
    "claude-sonnet-5":     (2.00, 10.00, 0.20),
    "gpt-5.6-sol":         (5.00, 30.00, 0.50),
    "gpt-5.6-terra":       (2.50, 15.00, 0.25),
    "gpt-5.6-luna":        (1.00, 6.00, 0.10),
    "kimi-k3":             (3.00, 15.00, 0.30),
    "grok-4.5":            (2.00, 6.00, 0.20),
    "qwen3.7-max":         (1.475, 4.425, 0.15),
    "glm-5.2":             (0.7616, 2.3936, 0.08),
    "deepseek-v4-pro":     (0.435, 0.87, 0.04),
    "deepseek-v4-flash":   (0.094, 0.188, 0.01),
    "qwen3-coder-next":    (0.110, 0.800, 0.01),
    "gpt-oss-120b":        (0.037, 0.170, 0.00),
    "gpt-oss-20b":         (0.030, 0.130, 0.00),
}


def lookup_price_per_m(model: str | None) -> tuple[float, float, float] | None:
    """Longest-suffix match so `openai/anthropic/claude-opus-5` resolves."""
    if not model:
        return None
    m = model.lower()
    best, best_len = None, -1
    for key, price in DEFAULT_PRICES_PER_M.items():
        if key in m and len(key) > best_len:
            best, best_len = price, len(key)
    return best


def price_call(
    tokens_in: int, tokens_out: int, cached_in: int = 0, *,
    model: str | None = None,
    litellm_cost: float | None = None,
    cost_per_token_in: float | None = None,
    cost_per_token_out: float | None = None,
) -> tuple[float | None, str]:
    """(usd, source). `source` is one of litellm | registered | table | unknown so the UI can
    say whether a number is actual or estimated instead of implying precision it lacks."""
    if litellm_cost is not None and litellm_cost > 0:
        return litellm_cost, "litellm"
    if cost_per_token_in is not None and cost_per_token_out is not None and (
        cost_per_token_in > 0 or cost_per_token_out > 0
    ):
        billable_in = max(0, tokens_in - cached_in)
        return billable_in * cost_per_token_in + tokens_out * cost_per_token_out, "registered"
    price = lookup_price_per_m(model)
    if price:
        pin, pout, pcache = price
        billable_in = max(0, tokens_in - cached_in)
        usd = (billable_in * pin + cached_in * pcache + tokens_out * pout) / 1e6
        return usd, "table"
    return None, "unknown"


# --- parsing the provider's usage block -------------------------------------------------
@dataclass(frozen=True)
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0
    cached_in: int = 0
    reasoning_out: int = 0

    @property
    def total(self) -> int:
        return self.tokens_in + self.tokens_out


def parse_usage(payload: dict | None) -> Usage | None:
    """Read an OpenAI-shaped `usage` block. Handles the nested *_details that carry cache
    and reasoning counts (verified present on our gateway)."""
    if not isinstance(payload, dict):
        return None
    u = payload.get("usage") if "usage" in payload else payload
    if not isinstance(u, dict) or not u:
        return None
    pd = u.get("prompt_tokens_details") or {}
    cd = u.get("completion_tokens_details") or {}
    return Usage(
        tokens_in=int(u.get("prompt_tokens") or u.get("input_tokens") or 0),
        tokens_out=int(u.get("completion_tokens") or u.get("output_tokens") or 0),
        cached_in=int((pd or {}).get("cached_tokens") or 0),
        reasoning_out=int((cd or {}).get("reasoning_tokens") or 0),
    )


def usage_from_sse(chunk_text: str) -> Usage | None:
    """Pull usage out of a streamed SSE body. Providers put it on the LAST data frame that
    carries one (requires stream_options.include_usage upstream); we scan all frames and keep
    the last non-empty, so a stream without it simply yields None."""
    found = None
    for line in chunk_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        blob = line[5:].strip()
        if not blob or blob == "[DONE]":
            continue
        try:
            u = parse_usage(json.loads(blob))
        except (ValueError, TypeError):
            continue
        if u and u.total:
            found = u
    return found


# --- the ledger -------------------------------------------------------------------------
@dataclass
class CallRecord:
    ts: float
    session: str
    squad: str | None
    model: str | None
    api_base: str | None
    deployment_id: str | None
    call_id: str | None
    billing: str
    tokens_in: int = 0
    tokens_out: int = 0
    cached_in: int = 0
    reasoning_out: int = 0
    usd: float | None = None
    cost_source: str = "unknown"
    duration_ms: float | None = None


@dataclass
class UsageLedger:
    """Bounded in-memory ledger. Deliberately not persisted: it is live telemetry for the
    current session, and persisting spend data is the operator's choice, not a default."""
    max_records: int = 5000
    _records: list[CallRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, rec: CallRecord) -> None:
        with self._lock:
            self._records.append(rec)
            if len(self._records) > self.max_records:
                del self._records[: len(self._records) - self.max_records]

    def records(self, session: str | None = None) -> list[CallRecord]:
        with self._lock:
            return [r for r in self._records if session in (None, r.session)]

    def rollup(self, session: str | None = None) -> dict:
        return summarize(self.records(session))

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


def summarize(records: list[CallRecord]) -> dict:
    """Fold calls into the shape the status bar and /fleet-usage need."""
    total = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cached_in": 0, "usd": 0.0}
    by_tier: dict[str, dict] = defaultdict(
        lambda: {"calls": 0, "tokens_in": 0, "tokens_out": 0, "usd": 0.0})
    by_billing: dict[str, dict] = defaultdict(
        lambda: {"calls": 0, "tokens_in": 0, "tokens_out": 0, "usd": 0.0})
    unpriced = 0

    for r in records:
        total["calls"] += 1
        total["tokens_in"] += r.tokens_in
        total["tokens_out"] += r.tokens_out
        total["cached_in"] += r.cached_in
        if r.usd is None:
            unpriced += 1
        else:
            total["usd"] += r.usd
        for bucket, key in ((by_tier, r.squad or "?"), (by_billing, r.billing)):
            b = bucket[key]
            b["calls"] += 1
            b["tokens_in"] += r.tokens_in
            b["tokens_out"] += r.tokens_out
            b["usd"] += r.usd or 0.0

    tok_total = total["tokens_in"] + total["tokens_out"]
    mix = {
        k: {
            **v,
            "pct_tokens": round(100 * (v["tokens_in"] + v["tokens_out"]) / tok_total, 1)
            if tok_total else 0.0,
            "pct_usd": round(100 * v["usd"] / total["usd"], 1) if total["usd"] else 0.0,
        }
        for k, v in by_billing.items()
    }
    return {
        "total": {**total, "usd": round(total["usd"], 6), "tokens": tok_total},
        "by_tier": {k: {**v, "usd": round(v["usd"], 6)} for k, v in by_tier.items()},
        "by_billing": {k: {**v, "usd": round(v["usd"], 6)} for k, v in mix.items()},
        "unpriced_calls": unpriced,
        "cache_hit_pct": round(100 * total["cached_in"] / total["tokens_in"], 1)
        if total["tokens_in"] else 0.0,
    }


def record_from(
    *, session: str, headers: dict, usage: Usage | None, model: str | None,
    declared_billing: str | None = None, price_in: float | None = None,
    price_out: float | None = None, now: float | None = None,
) -> CallRecord:
    """Build a priced CallRecord from what the response actually carried."""
    u = usage or Usage()
    api_base = headers.get("x-litellm-model-api-base")
    lc = headers.get("x-litellm-response-cost-original")
    try:
        litellm_cost = float(lc) if lc is not None else None
    except (TypeError, ValueError):
        litellm_cost = None
    usd, src = price_call(u.tokens_in, u.tokens_out, u.cached_in, model=model,
                          litellm_cost=litellm_cost, cost_per_token_in=price_in,
                          cost_per_token_out=price_out)
    dur = headers.get("x-litellm-response-duration-ms")
    try:
        duration = float(dur) if dur is not None else None
    except (TypeError, ValueError):
        duration = None
    return CallRecord(
        ts=now if now is not None else time.time(),
        session=session,
        squad=headers.get("x-dnc-squad"),
        model=model,
        api_base=api_base,
        deployment_id=headers.get("x-litellm-model-id"),
        call_id=headers.get("x-litellm-call-id"),
        billing=classify_billing(api_base, declared_billing),
        tokens_in=u.tokens_in, tokens_out=u.tokens_out,
        cached_in=u.cached_in, reasoning_out=u.reasoning_out,
        usd=usd, cost_source=src, duration_ms=duration,
    )


def as_dict(rec: CallRecord) -> dict:
    return asdict(rec)


#: Process-wide ledger the middleware writes and /dnc/usage reads.
LEDGER = UsageLedger()
