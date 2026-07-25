import json

from dnc_router import usage as U


# -- parsing what the gateway actually returns ---------------------------------------
#: Verbatim shape from a live call through our gateway (2026-07-25).
LIVE_BODY = {
    "usage": {
        "completion_tokens": 5, "prompt_tokens": 14, "total_tokens": 19,
        "completion_tokens_details": {"reasoning_tokens": 3},
        "prompt_tokens_details": {"cached_tokens": 0},
    }
}
LIVE_HEADERS = {
    "x-litellm-call-id": "e3307393-1eb7-4c1c-a6dc-e58de3be1339",
    "x-litellm-model-id": "62d2e306ec52",
    "x-litellm-model-api-base": "https://api.z.ai/api/paas/v4",
    "x-litellm-model-group": "tier:s0",
    "x-litellm-response-cost-original": "0.0",
    "x-litellm-response-duration-ms": "1884.006",
    "x-dnc-squad": "s0",
}


def test_parses_the_real_usage_block_including_details():
    u = U.parse_usage(LIVE_BODY)
    assert (u.tokens_in, u.tokens_out, u.cached_in, u.reasoning_out) == (14, 5, 0, 3)
    assert u.total == 19


def test_parses_cached_tokens_when_present():
    u = U.parse_usage({"usage": {"prompt_tokens": 1000, "completion_tokens": 10,
                                 "prompt_tokens_details": {"cached_tokens": 800}}})
    assert u.cached_in == 800


def test_parse_usage_tolerates_missing_or_junk():
    assert U.parse_usage(None) is None
    assert U.parse_usage({}) is None
    assert U.parse_usage({"usage": {}}) is None


def test_usage_from_sse_takes_the_last_frame_with_usage():
    sse = (
        'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        'data: {"usage":{"prompt_tokens":10,"completion_tokens":2}}\n\n'
        'data: {"usage":{"prompt_tokens":10,"completion_tokens":7}}\n\n'
        "data: [DONE]\n\n"
    )
    u = U.usage_from_sse(sse)
    assert (u.tokens_in, u.tokens_out) == (10, 7)


def test_usage_from_sse_returns_none_without_include_usage():
    assert U.usage_from_sse('data: {"choices":[{"delta":{"content":"x"}}]}\n\ndata: [DONE]\n\n') is None


# -- billing classification ------------------------------------------------------------
def test_private_addresses_are_local_hardware():
    for base in ("http://192.168.1.106:8080/v1", "http://10.0.0.4:8000/v1",
                 "http://127.0.0.1:8080/v1", "http://localhost:8080/v1"):
        assert U.classify_billing(base) == U.LOCAL


def test_public_api_defaults_to_per_token():
    assert U.classify_billing("https://openrouter.ai/api/v1") == U.PER_TOKEN


def test_declared_billing_always_wins():
    # only the operator knows an endpoint is behind a subscription
    assert U.classify_billing("https://api.anthropic.com", "subscription") == U.SUBSCRIPTION
    assert U.classify_billing("http://192.168.1.5:8000/v1", "per_hour") == U.PER_HOUR
    assert U.classify_billing("https://x.example", "nonsense") == U.PER_TOKEN


# -- pricing ----------------------------------------------------------------------------
def test_litellm_cost_wins_when_nonzero():
    usd, src = U.price_call(1000, 100, model="claude-opus-5", litellm_cost=0.0123)
    assert (usd, src) == (0.0123, "litellm")


def test_zero_litellm_cost_is_ignored_not_trusted():
    # the live gateway reports 0.0 because no prices are registered — don't record $0
    usd, src = U.price_call(1_000_000, 1_000_000, model="claude-opus-5", litellm_cost=0.0)
    assert src == "table" and usd == 5.0 + 25.0


def test_registered_per_token_prices_beat_the_table():
    usd, src = U.price_call(1_000_000, 0, model="claude-opus-5",
                            cost_per_token_in=1e-6, cost_per_token_out=2e-6)
    assert src == "registered" and usd == 1.0


def test_table_prices_discount_cached_input():
    full, _ = U.price_call(1_000_000, 0, 0, model="claude-opus-5")
    cached, _ = U.price_call(1_000_000, 0, 1_000_000, model="claude-opus-5")
    assert full == 5.0 and cached == 0.5      # cache rate, not the full input rate


def test_unknown_model_is_unpriced_not_guessed():
    usd, src = U.price_call(1000, 100, model="some/unheard-of-model")
    assert usd is None and src == "unknown"


def test_price_lookup_matches_longest_suffix():
    assert U.lookup_price_per_m("openai/anthropic/claude-opus-5")[0] == 5.00
    assert U.lookup_price_per_m("openai/openai/gpt-oss-120b")[1] == 0.170


# -- records + rollup ---------------------------------------------------------------------
def test_record_from_live_headers_is_fully_populated():
    r = U.record_from(session="main", headers=LIVE_HEADERS,
                      usage=U.parse_usage(LIVE_BODY), model="tier:s0", now=1000.0)
    assert r.squad == "s0" and r.billing == U.PER_TOKEN
    assert r.call_id == LIVE_HEADERS["x-litellm-call-id"]
    assert r.api_base == "https://api.z.ai/api/paas/v4"
    assert r.duration_ms == 1884.006
    assert r.tokens_in == 14 and r.tokens_out == 5 and r.reasoning_out == 3
    assert r.cost_source == "unknown"   # tier:s0 isn't a priced model string


def test_rollup_splits_by_tier_and_billing_with_percentages():
    led = U.UsageLedger()
    mk = lambda squad, base, tin, tout, billing=None: U.record_from(  # noqa: E731
        session="s1", headers={"x-dnc-squad": squad, "x-litellm-model-api-base": base},
        usage=U.Usage(tin, tout), model={"s0": "claude-opus-5", "s3": "gpt-oss-120b"}[squad],
        declared_billing=billing, now=1.0)
    led.add(mk("s0", "https://api.anthropic.com", 1_000_000, 200_000, "subscription"))
    led.add(mk("s3", "https://openrouter.ai/api/v1", 3_000_000, 600_000))

    r = led.rollup("s1")
    assert r["total"]["calls"] == 2
    assert r["total"]["tokens"] == 4_800_000
    assert set(r["by_tier"]) == {"s0", "s3"}
    # S0 is 25% of tokens but should dominate spend — the whole point of the display
    sub, ppt = r["by_billing"]["subscription"], r["by_billing"]["per_token"]
    assert sub["pct_tokens"] == 25.0 and ppt["pct_tokens"] == 75.0
    assert sub["pct_usd"] > 90.0


def test_rollup_reports_unpriced_calls_instead_of_hiding_them():
    led = U.UsageLedger()
    led.add(U.record_from(session="s", headers={}, usage=U.Usage(10, 10),
                          model="mystery-model", now=1.0))
    r = led.rollup("s")
    assert r["unpriced_calls"] == 1 and r["total"]["usd"] == 0.0


def test_rollup_computes_cache_hit_rate():
    led = U.UsageLedger()
    led.add(U.record_from(session="s", headers={},
                          usage=U.Usage(1000, 10, cached_in=750),
                          model="claude-opus-5", now=1.0))
    assert led.rollup("s")["cache_hit_pct"] == 75.0


def test_ledger_is_bounded_and_session_scoped():
    led = U.UsageLedger(max_records=10)
    for i in range(25):
        led.add(U.record_from(session="a" if i % 2 else "b", headers={},
                              usage=U.Usage(1, 1), model="gpt-oss-120b", now=float(i)))
    assert len(led.records()) == 10
    assert all(r.session == "a" for r in led.records("a"))


def test_empty_rollup_does_not_divide_by_zero():
    r = U.UsageLedger().rollup()
    assert r["total"]["calls"] == 0 and r["cache_hit_pct"] == 0.0
