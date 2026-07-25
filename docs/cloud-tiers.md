# Cloud tier reference — model selection & pricing

The tier ladder for **cloud-only mode**: which model each squad points at, why, and what it
costs. This is the cloud counterpart to [`model-selection-2026.md`](model-selection-2026.md)
(which covers VRAM math for self-hosted tiers).

> **Prices verified 2026-07-25** against live OpenRouter / DeepInfra / Groq pages. **They
> drift.** Re-check before quoting them anywhere that matters. Every figure below is $ per
> million tokens (`in` / `out`). Anything we could not read off a live page is marked
> UNVERIFIED and should not be budgeted against.

## The thesis in one table

The router maps request complexity to a squad (`low→s3, medium→s2, high→s1, max→s0`). Point
those four squads at four price points and the same work costs wildly different amounts:

| Tier | Complexity | Default pick | in $/M | out $/M | What it does |
|---|---|---|---|---|---|
| **S0** | `max` | `anthropic/claude-opus-5` | 5.00 | 25.00 | Architecture, hard debugging, code review, planning |
| **S1** | `high` | `anthropic/claude-sonnet-5` | 2.00 | 10.00 | Feature implementation, non-trivial refactors |
| **S2** | `medium` | `deepseek/deepseek-v4-flash` | 0.094 | 0.188 | Routine code, tests, straightforward edits |
| **S3** | `low` | `openai/gpt-oss-120b` | 0.037 | 0.170 | Docs, formatting, judge/classification, bulk |

**S0 → S3 is ~135× on input and ~147× on output.** That spread is the entire product.

## What it saves

A worked example. Assume a heavy month: **10M input + 2M output tokens**. Complexity mix is *illustrative* —
substitute your own; the point is the shape, not the exact percentages.

| Routing | Cost | Note |
|---|---|---|
| Everything on `claude-fable-5` | **$200.00** | 10×$10 + 2×$50 |
| Everything on `claude-opus-5` | **$100.00** | 10×$5 + 2×$25 |
| **Tiered** (10% max / 20% high / 40% medium / 30% low) | **$18.74** | breakdown below |

```
S0  10%   1.0M in / 0.2M out   1.0×5     + 0.2×25    = $10.00
S1  20%   2.0M in / 0.4M out   2.0×2     + 0.4×10    =  $8.00
S2  40%   4.0M in / 0.8M out   4.0×0.094 + 0.8×0.188 =  $0.53
S3  30%   3.0M in / 0.6M out   3.0×0.037 + 0.6×0.170 =  $0.21
                                                       -------
                                                        $18.74
```

**The number that matters: S2+S3 handle 70% of the tokens for $0.74 — under 4% of the
bill.** The frontier tier is 10% of the work and 53% of the spend, which is exactly where it
*should* sit. The failure mode this fixes is running that 70% through the frontier model
because it happened to be the default.

Two multipliers not modelled above, both in your favour:
- **Prompt caching** — OpenRouter advertises 60–80% cost reduction on repeated context.
  The router's prefix-hash affinity deliberately keeps a stable system-prompt head, so
  caching hits more often than it would with random routing.
- **Free S3** — see below; the bottom tier can genuinely be $0.

## Full candidate list

Swap any of these in by editing one `model:` line in the config. Grouped by tier, cheapest
capable option first within each group.

### S0 — frontier (`complexity max`)

| Slug | in | out | ctx | Note |
|---|---|---|---|---|
| `moonshotai/kimi-k3` | 3.00 | 15.00 | 1M | Cheapest frontier-class; 2.8T open-weight reasoner |
| `anthropic/claude-opus-5` | 5.00 | 25.00 | 1M | **Default.** Strong agentic/subagent coordination |
| `openai/gpt-5.6-sol` | 5.00 | 30.00 | 1.05M | OpenAI flagship; CLI/agentic + multi-step coding |
| `anthropic/claude-fable-5` | 10.00 | 50.00 | 1M | Long-horizon autonomous, self-correcting. No ZDR option |

`claude-opus-5-fast` and `gpt-5.6-sol-pro` exist at 2× / same list price respectively —
only worth it if you specifically need the latency profile. `claude-opus-4.8` is priced
identically to Opus 5; there's no reason to route to it.

### S1 — strong mid (`complexity high`)

| Slug | in | out | ctx | Note |
|---|---|---|---|---|
| `deepseek/deepseek-v4-pro` | 0.435 | 0.87 | 1.05M | Outrageous price/capability. 4× cheaper on OR than Together/Fireworks |
| `z-ai/glm-5.2` | 0.762 | 2.394 | 1.05M | Very cheap for its class; good budget S1 |
| `qwen/qwen3.7-max` | 1.475 | 4.425 | 1M | Alibaba's top Qwen |
| `x-ai/grok-4.5` | 2.00 | 6.00 | 500K | Frontier coding claims at S1 price; smallest ctx here |
| `anthropic/claude-sonnet-5` | 2.00 | 10.00 | 1M | **Default.** Best price/perf in the Anthropic line |
| `openai/gpt-5.6-terra` | 2.50 | 15.00 | 1.05M | Mid GPT-5.6 tier |
| `google/gemini-3.1-pro-preview` | 2.00 | 12.00 | 1.05M | Only priced Gemini Pro currently listed |

### S2 — routine (`complexity medium`)

| Slug | in | out | ctx | Note |
|---|---|---|---|---|
| `qwen/qwen3.5-flash-02-23` | 0.065 | 0.260 | 1M | Newest cheap Qwen, 1M ctx |
| `qwen/qwen3-235b-a22b-2507` | 0.090 | 0.550 | 262K | Big-model quality at S2 price |
| `deepseek/deepseek-v4-flash` | 0.094 | 0.188 | **1M** | **Default.** Best price/capability on the list |
| `meta-llama/llama-4-scout` | 0.100 | 0.300 | 1.31M | Cheap huge context, mediocre coder |
| `qwen/qwen3-coder-next` | 0.110 | 0.800 | 262K | **Best S2 coding model under $0.15 in** |
| `z-ai/glm-4.5-air` | 0.130 | 0.850 | 131K | Well-proven agentic coder |
| `minimax/minimax-m2.5` | 0.150 | 0.900 | 205K | Cheapest good MiniMax agentic model |

### S3 — bulk (`complexity low`)

| Slug | in | out | ctx | Note |
|---|---|---|---|---|
| `openai/gpt-oss-20b:free` | **0** | **0** | 131K | FREE. Best free coding option — rate-limited, best-effort |
| `inclusionai/ling-3.0-flash:free` | **0** | **0** | 262K | FREE, large-ctx MoE |
| `cohere/north-mini-code:free` | **0** | **0** | 256K | FREE, code-tuned |
| `inclusionai/ling-2.6-flash` | 0.010 | 0.030 | 262K | Cheapest paid model on OR. Mechanical edits only |
| `openai/gpt-oss-20b` | 0.030 | 0.130 | 131K | Reliable tool-calling + tests |
| `openai/gpt-oss-120b` | 0.037 | 0.170 | 131K | **Default. Standout value** — near-20b price, much stronger |
| `qwen/qwen3-30b-a3b-instruct-2507` | 0.048 | 0.193 | 262K | Fast general coder (3B active) |
| `qwen/qwen3-coder-30b-a3b-instruct` | 0.070 | 0.270 | 262K | **Best S3 coding pick** — writes files to spec well |

**Free tier caveat:** OpenRouter lists ~18 zero-cost models, 5–6 coding-capable. They're
rate-limited and route to whoever donates capacity, so treat them as **best-effort with a
paid fallback** — never as your only S3. The gateway's existing `fallbacks` + cooldown
handles this cleanly (a rate-limited free model fails → cools down → traffic moves to the
paid sibling). DeepInfra has **no** free tier; its floor is ~$0.019.

## Provider notes

**Buy open weights on OpenRouter.** For identical open-weight models, OpenRouter was
cheapest in nearly every comparison — `gpt-oss-120b` at 0.037/0.17 vs **0.15/0.60** on
Together, Fireworks *and* Groq (a 4× spread), and `deepseek-v4-pro` at 0.435/0.87 vs
1.74/3.48. Together and Fireworks were consistently the most expensive for open weights.

**Buy latency on Groq, deliberately.** Groq is the only provider publishing throughput
(`gpt-oss-20b` 1,000 tok/s, `llama-3.1-8b` 840, `gpt-oss-120b` 500). You pay ~2.5× the
OpenRouter rate for the same model. Worth registering as a *latency override* for
interactive work, not as the bulk default. All other providers' speed claims: UNVERIFIED.

**OpenAI-compatible `/v1` base URLs** (what LiteLLM needs) — OpenRouter
`https://openrouter.ai/api/v1`, DeepInfra `https://api.deepinfra.com/v1/openai`, Groq
`https://api.groq.com/openai/v1`, Together `https://api.together.xyz/v1`, Fireworks
`https://api.fireworks.ai/inference/v1`, Novita `https://api.novita.ai/v3/openai`, Cerebras
`https://api.cerebras.ai/v1`. Confirm each in the provider's own docs before wiring — these
paths were not all re-verified on the day.

**Not usable / no rate card found:** Cerebras renders pricing client-side and returned no
table (a third-party aggregator claims `gpt-oss-120b` at $0.35/$0.75 — do **not** budget
against that). Hyperbolic's pricing page 404s and its console shows only GPU rental; it may
have exited serverless inference.

## OpenRouter features worth exploiting from the gateway

These are the reason OpenRouter is the recommended default aggregator rather than just a
convenient billing wrapper:

- **`max_price`** — a hard $/M ceiling per request. This is the clean way to *enforce* a
  tier budget at the provider layer instead of trusting the model list to stay cheap.
- **`provider.order` / `only` / `ignore` / `allow_fallbacks:false`** — pin or exclude
  specific upstreams; build an explicit fallback chain.
- **`quantizations`** — filter to int4…fp32. Matters for open weights: a provider quietly
  serving fp8 will underperform the same "model" elsewhere.
- **`:floor` / `:nitro`** shortcuts — cheapest vs highest-throughput provider for a model
  (`sort: price` / `throughput`; `latency` also available).
- **`partition:"none"`** — makes sorting evaluate endpoints *globally across your whole
  fallback model list* rather than per-model, i.e. genuine cheapest-across-tier routing.
- **`preferred_min_throughput` / `preferred_max_latency`** with p50–p99 percentiles.
- **`data_collection:"deny"` / `zdr:true`** for compliance routing. Note `claude-fable-5`
  has **no ZDR option** — data retention is a condition of use.
- **BYOK** — attach your own provider keys; OpenRouter charges 5% of the equivalent cost
  (waived for the first 1M BYOK requests/month). Keys can be marked prioritized vs fallback.
- **`openrouter/auto`** — meta-router picks among ~38 candidates, billed at the routed
  model's rate. Useful as an S1 catch-all, but you give up cost predictability, which is the
  whole point of tiering. Not recommended as a squad default.

## On subscriptions as the S0 tier

The motivating use case is "I'm burning my max-tier subscription on low-complexity work."
The cleanest fix is **leaving the subscription where it is** (your interactive editor) and
pointing *this* gateway at pay-per-token endpoints for everything the agent does
autonomously — that's the configuration above, and it needs no bridging at all.

Routing a *subscription* through the gateway as the S0 tier is a different, harder thing.
Subscription plans authenticate through their vendor's CLI/app, not an API key, so it
requires an auth bridge — that's what [`cliproxy/`](../cliproxy/) is for. Be aware:

- **It doesn't always work.** Our own attempt to bridge a Grok CLI subscription failed (the
  binary resists interception); we fell back to a pay-per-token key.
- **Check the provider's terms.** Using a subscription to serve an automated gateway may not
  be permitted, and per-plan rules differ. That's a decision for you, not a default we ship.

Pay-per-token S0 is the supported, reliable default. Subscription bridging is opt-in,
best-effort, and yours to validate.

## Re-checking prices

```bash
# full catalog with pricing, as JSON (per-token decimals — multiply by 1e6 for $/M)
curl -s https://openrouter.ai/api/v1/models | jq -r \
  '.data[] | [.id, (.pricing.prompt|tonumber*1e6), (.pricing.completion|tonumber*1e6)] | @tsv'
```

Sources: [OpenRouter models API](https://openrouter.ai/api/v1/models) ·
[provider routing](https://openrouter.ai/docs/features/provider-routing) ·
[BYOK](https://openrouter.ai/docs/use-cases/byok) ·
[DeepInfra pricing](https://deepinfra.com/pricing) ·
[Groq pricing](https://groq.com/pricing) ·
[Together pricing](https://www.together.ai/pricing) ·
[Fireworks pricing](https://docs.fireworks.ai/serverless/pricing) ·
[Novita pricing](https://novita.ai/pricing)
