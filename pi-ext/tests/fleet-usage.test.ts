import assert from "node:assert/strict";
import { test } from "node:test";

const {
  fmtUsd, fmtTokens, formatCall, providerLabel, formatSession,
  computeAdvice, formatUsageReport, advisorFromEnv, DEFAULT_ADVISOR,
} = await import("../src/fleet-usage.ts");

type Roll = Parameters<typeof formatSession>[0];

function roll(over: Partial<Roll> = {}): Roll {
  return {
    total: { calls: 10, tokens_in: 900_000, tokens_out: 100_000, cached_in: 0, tokens: 1_000_000, usd: 5 },
    by_tier: { s0: { calls: 10, tokens_in: 900_000, tokens_out: 100_000, usd: 5 } },
    by_billing: {
      subscription: { calls: 10, tokens_in: 900_000, tokens_out: 100_000, usd: 5, pct_tokens: 100, pct_usd: 100 },
    },
    unpriced_calls: 0,
    cache_hit_pct: 0,
    ...over,
  } as Roll;
}

// -- formatting -------------------------------------------------------------------
test("fmtUsd keeps sub-cent amounts visible instead of rounding to $0.00", () => {
  assert.equal(fmtUsd(0), "$0");
  assert.equal(fmtUsd(0.0012), "$0.0012");
  assert.equal(fmtUsd(0.25), "$0.250");
  assert.equal(fmtUsd(12.5), "$12.50");
});

test("fmtTokens abbreviates", () => {
  assert.equal(fmtTokens(950), "950");
  assert.equal(fmtTokens(12_300), "12.3k");
  assert.equal(fmtTokens(4_800_000), "4.8M");
});

test("providerLabel keeps host:port for LAN nodes, trims api. for hosted", () => {
  assert.equal(providerLabel("http://192.168.1.106:8080/v1"), "192.168.1.106:8080");
  assert.equal(providerLabel("https://api.z.ai/api/paas/v4"), "z.ai");
  assert.equal(providerLabel(undefined), undefined);
});

test("formatCall shows tier and where it ran", () => {
  assert.equal(formatCall("tier:s3", "192.168.1.106:8080", "s3-node-01"),
    "→ tier:s3 · 192.168.1.106:8080");
  assert.equal(formatCall("tier:s0", undefined, "s0-opus5"), "→ tier:s0 · s0-opus5");
});

test("formatSession shows calls, tokens, spend and the billing mix", () => {
  const s = formatSession(roll({
    by_billing: {
      subscription: { calls: 3, tokens_in: 200_000, tokens_out: 50_000, usd: 4, pct_tokens: 25, pct_usd: 80 },
      per_token: { calls: 7, tokens_in: 700_000, tokens_out: 50_000, usd: 1, pct_tokens: 75, pct_usd: 20 },
    },
  }));
  assert.match(s, /10 calls/);
  assert.match(s, /1\.0M tok/);
  assert.match(s, /ppt 75% \/ sub 25%/); // biggest share first
});

test("formatSession marks the total as approximate when calls are unpriced", () => {
  assert.match(formatSession(roll({ unpriced_calls: 2 })), /~\$/);
  assert.doesNotMatch(formatSession(roll({ unpriced_calls: 0 })), /~\$/);
});

test("formatSession surfaces the cache hit rate when there is one", () => {
  assert.match(formatSession(roll({ cache_hit_pct: 62 })), /cache 62%/);
  assert.doesNotMatch(formatSession(roll({ cache_hit_pct: 0 })), /cache/);
});

test("formatSession handles an empty session", () => {
  assert.equal(formatSession(roll({
    total: { calls: 0, tokens_in: 0, tokens_out: 0, cached_in: 0, tokens: 0, usd: 0 },
    by_billing: {}, by_tier: {},
  })), "fleet: no calls yet");
});

// -- the advisor ------------------------------------------------------------------
test("stays silent until there is enough evidence", () => {
  const a = computeAdvice(roll({ total: { ...roll().total, calls: 3 } }));
  assert.equal(a.level, "ok");
  assert.equal(a.lines.length, 0);
});

test("subscription-only session gets a notice, and admits it cannot project without a limit", () => {
  const a = computeAdvice(roll(), { ...DEFAULT_ADVISOR, sessionTokenLimit: undefined });
  assert.equal(a.level, "notice");
  assert.match(a.lines.join(" "), /ran on your subscription tier/);
  assert.match(a.lines.join(" "), /can't be projected/);
});

test("warns with a concrete multiplier once past the limit threshold", () => {
  const a = computeAdvice(roll(), { ...DEFAULT_ADVISOR, sessionTokenLimit: 1_200_000 });
  assert.equal(a.level, "warn");
  const text = a.lines.join(" ");
  assert.match(text, /83% of your 1\.2M-token session limit/);
  assert.match(text, /3\.3x more work/);      // 100/(100-70)
  assert.match(text, /fleet\/tier:auto/);      // actionable next step
});

test("does not warn when comfortably under the limit", () => {
  const a = computeAdvice(roll(), { ...DEFAULT_ADVISOR, sessionTokenLimit: 100_000_000 });
  assert.equal(a.level, "notice");
  assert.doesNotMatch(a.lines.join(" "), /will hit the limit/);
});

test("congratulates instead of nagging once work is delegated", () => {
  const a = computeAdvice(roll({
    by_tier: {
      s0: { calls: 2, tokens_in: 200_000, tokens_out: 100_000, usd: 4.5 },
      s3: { calls: 8, tokens_in: 700_000, tokens_out: 0, usd: 0.5 },
    },
    by_billing: {
      subscription: { calls: 2, tokens_in: 200_000, tokens_out: 100_000, usd: 4.5, pct_tokens: 30, pct_usd: 90 },
      per_token: { calls: 8, tokens_in: 700_000, tokens_out: 0, usd: 0.5, pct_tokens: 70, pct_usd: 10 },
    },
  }), { ...DEFAULT_ADVISOR, sessionTokenLimit: 1_000_000 });
  assert.equal(a.level, "ok");
  assert.match(a.lines.join(" "), /Tiering is working: 70% of tokens/);
});

test("advisorFromEnv reads the limit and ignores junk", () => {
  assert.equal(advisorFromEnv({ DNC_SUBSCRIPTION_SESSION_LIMIT: "2000000" } as NodeJS.ProcessEnv)
    .sessionTokenLimit, 2_000_000);
  assert.equal(advisorFromEnv({ DNC_SUBSCRIPTION_SESSION_LIMIT: "nope" } as NodeJS.ProcessEnv)
    .sessionTokenLimit, undefined);
  assert.equal(advisorFromEnv({} as NodeJS.ProcessEnv).sessionTokenLimit, undefined);
});

// -- the report -------------------------------------------------------------------
test("report breaks down tier and billing, and flags unpriced calls", () => {
  const lines = formatUsageReport(roll({ unpriced_calls: 3 }), { level: "ok", lines: [] }).join("\n");
  assert.match(lines, /by tier:/);
  assert.match(lines, /s0/);
  assert.match(lines, /by billing:/);
  assert.match(lines, /register input_cost_per_token/);
});

test("report appends advice when there is any", () => {
  const lines = formatUsageReport(roll(), { level: "warn", lines: ["do the thing"] }).join("\n");
  assert.match(lines, /do the thing/);
});
