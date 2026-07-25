/**
 * Live cost/usage telemetry: what each call cost, where it ran, and how the session is
 * splitting between subscription, pay-per-token, pay-per-hour and hardware you own.
 *
 * Why the numbers come from the gateway rather than from Pi: `after_provider_response`
 * carries only `{status, headers}`, and no extension event exposes token counts. So the
 * router's middleware captures the provider's own `usage` block (including `cached_tokens`),
 * prices it, and serves a rollup at `GET /dnc/usage` which we poll. Headers still give us the
 * *instant* signal — tier, provider, deployment — so the per-call line updates immediately
 * and the totals catch up a beat later.
 *
 * The end goal is behavioural, not decorative: make it obvious when the frontier tier is
 * eating a subscription that cheaper tiers could have absorbed.
 */
import type { ExtensionAPI, ExtensionUIContext } from "@earendil-works/pi-coding-agent";

/** Resolved lazily rather than imported from ./config.js so this module's static graph stays
 *  loadable by the unit tests (same reason last-tier.ts dynamically imports its ledger). */
function gatewayUrl(): string {
  return process.env.DNC_LITELLM_URL ?? "http://localhost:4000";
}

export interface BillingBucket {
  calls: number;
  tokens_in: number;
  tokens_out: number;
  usd: number;
  pct_tokens: number;
  pct_usd: number;
}
export interface UsageRollup {
  total: { calls: number; tokens_in: number; tokens_out: number; cached_in: number; tokens: number; usd: number };
  by_tier: Record<string, { calls: number; tokens_in: number; tokens_out: number; usd: number }>;
  by_billing: Record<string, BillingBucket>;
  unpriced_calls: number;
  cache_hit_pct: number;
}

const BILLING_LABEL: Record<string, string> = {
  subscription: "sub",
  per_token: "ppt",
  per_hour: "hr",
  local: "local",
};

/** Compact money: sub-cent amounts still need to be visible, not rounded to $0.00. */
export function fmtUsd(usd: number): string {
  if (usd === 0) return "$0";
  if (usd < 0.01) return `$${usd.toFixed(4)}`;
  if (usd < 1) return `$${usd.toFixed(3)}`;
  return `$${usd.toFixed(2)}`;
}

export function fmtTokens(n: number): string {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(n);
}

/** Per-call line, shown the instant the response headers land. */
export function formatCall(
  tier: string, provider: string | undefined, deployId: string | undefined,
): string {
  const where = provider ?? deployId ?? "?";
  return `→ ${tier} · ${where}`;
}

/** Shorten an api_base to something that fits a status bar. */
export function providerLabel(apiBase: string | undefined): string | undefined {
  if (!apiBase) return undefined;
  try {
    const u = new URL(apiBase);
    // keep host:port for LAN nodes; for hosted APIs the bare host is enough
    return u.port ? `${u.hostname}:${u.port}` : u.hostname.replace(/^api\./, "");
  } catch {
    return apiBase;
  }
}

/** The running session total for the bottom bar. */
export function formatSession(r: UsageRollup): string {
  if (!r.total.calls) return "fleet: no calls yet";
  const mix = Object.entries(r.by_billing)
    .filter(([, b]) => b.pct_tokens > 0)
    .sort((a, b) => b[1].pct_tokens - a[1].pct_tokens)
    .map(([k, b]) => `${BILLING_LABEL[k] ?? k} ${Math.round(b.pct_tokens)}%`)
    .join(" / ");
  const cache = r.cache_hit_pct > 0 ? ` · cache ${Math.round(r.cache_hit_pct)}%` : "";
  const est = r.unpriced_calls > 0 ? "~" : "";
  return `fleet: ${r.total.calls} calls · ${fmtTokens(r.total.tokens)} tok · ${est}${fmtUsd(r.total.usd)} · ${mix}${cache}`;
}

// --- the advisor ----------------------------------------------------------------------
export interface AdvisorConfig {
  /** Subscription session token cap, if the user knows it. Without it we can still report
   *  the mix, but we cannot project exhaustion — so we say so rather than guess. */
  sessionTokenLimit?: number;
  warnAtPct: number;
  minCalls: number;
  /** Share of tokens a tiered setup typically moves off the top tier. Used ONLY when this
   *  user has no observed tiered mix of their own. Measured claim from docs/cloud-tiers.md. */
  typicalOffloadPct: number;
}

export const DEFAULT_ADVISOR: AdvisorConfig = {
  sessionTokenLimit: undefined,
  warnAtPct: 70,
  minCalls: 8,
  typicalOffloadPct: 70,
};

export interface Advice {
  level: "ok" | "notice" | "warn";
  lines: string[];
}

/**
 * Decide whether to nudge the user toward tiering.
 *
 * Deliberately quiet at first: we only speak up once there is enough evidence (minCalls),
 * and we never invent a limit. When the user is already tiering we report the efficiency
 * instead of nagging.
 */
export function computeAdvice(r: UsageRollup, cfg: AdvisorConfig = DEFAULT_ADVISOR): Advice {
  if (r.total.calls < cfg.minCalls) {
    return { level: "ok", lines: [] };
  }
  const sub = r.by_billing.subscription;
  const subTokens = sub ? sub.tokens_in + sub.tokens_out : 0;
  const tiers = Object.keys(r.by_tier).filter((t) => t !== "?");
  const delegated = 100 - (r.by_tier.s0
    ? (100 * (r.by_tier.s0.tokens_in + r.by_tier.s0.tokens_out)) / r.total.tokens
    : 0);

  // Already delegating: report the win rather than nagging.
  if (tiers.length > 1 && delegated >= 25) {
    return {
      level: "ok",
      lines: [
        `Tiering is working: ${Math.round(delegated)}% of tokens served below the frontier tier.`,
        ...(r.total.usd > 0 ? [`Session spend ${fmtUsd(r.total.usd)} across ${r.total.calls} calls.`] : []),
      ],
    };
  }

  // Everything on one (subscription) tier — the case worth flagging.
  if (subTokens > 0 && (tiers.length <= 1 || delegated < 25)) {
    const lines = [
      `All ${fmtTokens(r.total.tokens)} tokens this session ran on your subscription tier.`,
    ];
    let level: Advice["level"] = "notice";

    if (cfg.sessionTokenLimit && cfg.sessionTokenLimit > 0) {
      const pct = (100 * subTokens) / cfg.sessionTokenLimit;
      lines.push(
        `That is ${pct.toFixed(0)}% of your ${fmtTokens(cfg.sessionTokenLimit)}-token session limit.`,
      );
      if (pct >= cfg.warnAtPct) {
        level = "warn";
        const extended = 100 / Math.max(1, 100 - cfg.typicalOffloadPct);
        lines.push(
          `At this rate you will hit the limit before the session ends. Enabling tiers ` +
          `typically moves ~${cfg.typicalOffloadPct}% of tokens to cheaper models — roughly ` +
          `${extended.toFixed(1)}x more work within the same subscription cap.`,
          `Try: set model fleet/tier:auto, then /complexity low|medium for routine turns.`,
        );
      }
    } else {
      lines.push(
        `No subscription limit configured, so exhaustion can't be projected — set ` +
        `DNC_SUBSCRIPTION_SESSION_LIMIT to enable that.`,
        `Enabling tiers typically moves ~${cfg.typicalOffloadPct}% of tokens off the top tier.`,
      );
    }
    return { level, lines };
  }
  return { level: "ok", lines: [] };
}

export function formatUsageReport(r: UsageRollup, advice: Advice): string[] {
  const lines = [
    `Fleet usage — ${r.total.calls} calls · ${fmtTokens(r.total.tokens)} tokens · ` +
    `${r.unpriced_calls ? "~" : ""}${fmtUsd(r.total.usd)}`,
  ];
  if (r.total.cached_in) {
    lines.push(`  cache: ${fmtTokens(r.total.cached_in)} cached input (${r.cache_hit_pct}% of input)`);
  }
  lines.push("by tier:");
  for (const [tier, b] of Object.entries(r.by_tier).sort()) {
    lines.push(`  ${tier.padEnd(6)} ${String(b.calls).padStart(4)} calls  ` +
      `${fmtTokens(b.tokens_in + b.tokens_out).padStart(7)} tok  ${fmtUsd(b.usd)}`);
  }
  lines.push("by billing:");
  for (const [k, b] of Object.entries(r.by_billing).sort((a, c) => c[1].pct_usd - a[1].pct_usd)) {
    lines.push(`  ${(BILLING_LABEL[k] ?? k).padEnd(6)} ${b.pct_tokens.toFixed(0).padStart(3)}% tokens  ` +
      `${b.pct_usd.toFixed(0).padStart(3)}% spend  ${fmtUsd(b.usd)}`);
  }
  if (r.unpriced_calls) {
    lines.push(`  (${r.unpriced_calls} call(s) unpriced — register input_cost_per_token in the ` +
      `gateway config for exact figures)`);
  }
  if (advice.lines.length) {
    lines.push("", ...advice.lines);
  }
  return lines;
}

async function fetchRollup(session: string): Promise<UsageRollup | undefined> {
  try {
    const res = await fetch(`${gatewayUrl()}/dnc/usage?session=${encodeURIComponent(session)}`);
    if (!res.ok) return undefined;
    return (await res.json()) as UsageRollup;
  } catch {
    return undefined; // telemetry must never break a turn
  }
}

export function registerFleetUsage(pi: ExtensionAPI): void {
  let lastRollup: UsageRollup | undefined;
  let advisedAt = 0;

  pi.on("after_provider_response", async (event, ctx) => {
    if (ctx.model?.provider !== "fleet") return;
    const headers = (event.headers ?? {}) as Record<string, string>;
    const tier = headers["x-dnc-squad"]
      ? `tier:${headers["x-dnc-squad"]}`
      : (headers["x-litellm-model-group"] ?? ctx.model?.id ?? "?");
    const provider = providerLabel(headers["x-litellm-model-api-base"]);

    // Instant per-call line from headers — no round-trip needed.
    ctx.ui.setStatus("dnc-call", formatCall(tier, provider, headers["x-litellm-model-id"]));

    // Totals lag one beat: the gateway can only price the call once its body has streamed.
    const session = headers["x-dnc-session"] ?? "main";
    const roll = await fetchRollup(session);
    if (!roll) return;
    lastRollup = roll;
    ctx.ui.setStatus("dnc-usage", formatSession(roll));

    // Nudge at most once every 20 calls so it informs rather than nags.
    const advice = computeAdvice(roll, advisorFromEnv());
    if (advice.level === "warn" && roll.total.calls - advisedAt >= 20) {
      advisedAt = roll.total.calls;
      ctx.ui.notify(advice.lines[0] ?? "subscription usage is high", "error");
    }
  });

  pi.registerCommand("fleet-usage", {
    description: "Session cost/usage: tokens, spend, and the subscription vs pay-per-token mix",
    handler: async (args, ctx: { ui: ExtensionUIContext }) => {
      if ((args ?? "").trim() === "reset") {
        try {
          await fetch(`${gatewayUrl()}/dnc/usage/reset`, { method: "POST" });
          ctx.ui.notify("usage ledger reset", "info");
        } catch {
          ctx.ui.notify("could not reach the gateway to reset usage", "error");
        }
        return;
      }
      const roll = (await fetchRollup("main")) ?? lastRollup;
      if (!roll) {
        ctx.ui.notify(
          "no usage data — is the gateway running dnc_router.serve with /dnc/usage?", "error");
        return;
      }
      ctx.ui.setWidget("dnc-usage-report",
        formatUsageReport(roll, computeAdvice(roll, advisorFromEnv())));
    },
  });
}

export function advisorFromEnv(env: NodeJS.ProcessEnv = process.env): AdvisorConfig {
  const lim = Number(env.DNC_SUBSCRIPTION_SESSION_LIMIT ?? "");
  return {
    ...DEFAULT_ADVISOR,
    sessionTokenLimit: Number.isFinite(lim) && lim > 0 ? lim : undefined,
    warnAtPct: Number(env.DNC_SUBSCRIPTION_WARN_PCT ?? DEFAULT_ADVISOR.warnAtPct),
  };
}
