/**
 * `/fleet-setup` — get from "nothing configured" to "tiered routing working" without
 * opening a text editor.
 *
 * Setting this up by hand has three steps that are each easy to get subtly wrong: the API
 * key has to land in the gateway's env file (exporting it in your shell does not survive a
 * restart), the tier→model mapping lives in YAML, and nothing tells you a model slug was
 * mistyped until a task fails. So fleetd owns all three and this command drives it, ending
 * with a live probe of every tier.
 *
 *   /fleet-setup                     guided: provider → key → defaults → apply → verify
 *   /fleet-provider                  show providers and which ones have a key
 *   /fleet-provider openrouter <key> set a key non-interactively
 *   /fleet-model                     browse and change the model behind a tier
 *   /fleet-model s2 deepseek/deepseek-v4-flash
 *   /fleet-verify                    probe every tier with a 1-token completion
 */
import type { ExtensionAPI, ExtensionUIContext } from "@earendil-works/pi-coding-agent";
import { fleetdGet, fleetdSend } from "./config.js";

interface ProviderRow {
  key: string; label: string; api_base: string; key_env: string; signup: string;
  has_closed_models: boolean; note: string; key_present: boolean;
}
interface ProvidersResp { current: string; providers: ProviderRow[] }
interface CatalogEntry { model: string; in: number; out: number; default?: boolean; note?: string }
interface CatalogResp { provider: string; catalog: Record<string, CatalogEntry[]>; defaults: Record<string, string> }
interface TiersResp { provider: string; tiers: Record<string, string>; key_present: boolean; key_env: string | null; path: string }
interface VerifyResp { provider: string; all_ok: boolean; results: { tier: string; model: string; ok: boolean; detail: string }[] }

const TIERS = ["s0", "s1", "s2", "s3"] as const;
const TIER_JOB: Record<string, string> = {
  s0: "max — architecture, hard debugging, review",
  s1: "high — feature implementation, refactors",
  s2: "medium — routine code, tests",
  s3: "low — docs, formatting, bulk",
};

export function priceLabel(e: CatalogEntry): string {
  if (e.in === 0 && e.out === 0) return "FREE";
  return `$${e.in}/$${e.out} per M`;
}

export function tierLines(tiers: Record<string, string>): string[] {
  return TIERS.filter((t) => tiers[t]).map(
    (t) => `  ${t.toUpperCase()}  ${tiers[t]}`);
}

export function verifyLines(v: VerifyResp): string[] {
  return [
    `Tier probe — ${v.provider}${v.all_ok ? " — all tiers answered ✅" : " — some tiers failed ❌"}`,
    ...v.results.map((r) => `  ${r.ok ? "✅" : "❌"} tier:${r.tier}  ${r.model}` +
      (r.ok ? "" : `\n       ${r.detail}`)),
  ];
}

async function pickProvider(ui: ExtensionUIContext): Promise<ProviderRow | undefined> {
  const resp = await fleetdGet<ProvidersResp>("/providers");
  const labels = resp.providers.map((p) =>
    `${p.label}${p.key_present ? " (key set)" : ""} — ${p.has_closed_models
      ? "frontier + open models" : "open weights only"}`);
  const choice = await ui.select("Which provider should back your tiers?", labels);
  if (choice === undefined) return undefined;
  return resp.providers[labels.indexOf(choice)];
}

async function ensureKey(p: ProviderRow, ui: ExtensionUIContext): Promise<boolean> {
  if (p.key_present) {
    const replace = await ui.confirm(
      `${p.label} already has a key on the gateway`, "Replace it?");
    if (!replace) return true;
  }
  ui.setWidget("dnc-setup", [
    `Get a ${p.label} API key: ${p.signup}`,
    `It will be written to the gateway's env file as ${p.key_env} (chmod 600).`,
    `It is never logged or echoed back.`,
  ]);
  const key = await ui.input(`Paste your ${p.label} API key`, "");
  if (!key) {
    ui.notify("no key entered — skipping", "info");
    return p.key_present;
  }
  try {
    const r = await fleetdSend<{ masked: string; env_var: string }>(
      "POST", `/providers/${p.key}/key`, { api_key: key.trim() });
    ui.notify(`saved ${r.env_var} = ${r.masked}`, "info");
    return true;
  } catch (err) {
    ui.notify(`could not save the key: ${err}`, "error");
    return false;
  }
}

async function chooseTierModel(
  provider: string, tier: string, ui: ExtensionUIContext,
): Promise<string | undefined> {
  const cat = await fleetdGet<CatalogResp>(`/providers/${provider}/catalog`);
  const opts = cat.catalog[tier] ?? [];
  if (!opts.length) {
    ui.notify(`no catalog entries for ${tier} on ${provider}`, "error");
    return undefined;
  }
  const labels = opts.map((e) =>
    `${e.model} — ${priceLabel(e)}${e.default ? " (default)" : ""}${e.note ? ` · ${e.note}` : ""}`);
  const pick = await ui.select(`tier:${tier} — ${TIER_JOB[tier]}`, labels);
  return pick === undefined ? undefined : opts[labels.indexOf(pick)].model;
}

async function applyAndVerify(ui: ExtensionUIContext, body: Record<string, unknown>): Promise<void> {
  ui.setWidget("dnc-setup", ["Writing config and restarting the gateway…"]);
  let applied: TiersResp & { restarted?: boolean; restart_error?: string };
  try {
    applied = await fleetdSend("POST", "/tiers", { ...body, apply: true, restart: true });
  } catch (err) {
    ui.notify(`apply failed: ${err}`, "error");
    return;
  }
  const head = [
    `Applied — provider ${applied.provider}`,
    ...tierLines(applied.tiers),
    applied.restarted ? "  gateway restarted" : `  ⚠️ restart failed: ${applied.restart_error ?? "?"}`,
    "",
    "Probing each tier with a 1-token completion…",
  ];
  ui.setWidget("dnc-setup", head);
  // The gateway needs a moment to come back before it can answer a probe.
  await new Promise((r) => setTimeout(r, 8000));
  try {
    const v = await fleetdSend<VerifyResp>("POST", "/tiers/verify");
    ui.setWidget("dnc-setup", [...head.slice(0, -2), ...verifyLines(v), "",
      v.all_ok
        ? "Ready. In Pi pick model fleet/tier:auto, then /complexity low|medium|high|max."
        : "Fix the failing tiers with /fleet-model <tier>, then /fleet-verify."]);
    ui.notify(v.all_ok ? "fleet ready — all tiers answered" : "some tiers failed to answer",
      v.all_ok ? "info" : "error");
  } catch (err) {
    ui.notify(`verify failed: ${err}`, "error");
  }
}

export function registerFleetSetup(pi: ExtensionAPI): void {
  pi.registerCommand("fleet-setup", {
    description: "Guided setup: pick a provider, add its API key, choose tier models, apply + verify",
    handler: async (_args, ctx) => {
      const p = await pickProvider(ctx.ui);
      if (!p) return;
      if (!(await ensureKey(p, ctx.ui))) return;

      const cat = await fleetdGet<CatalogResp>(`/providers/${p.key}/catalog`);
      const useDefaults = await ctx.ui.confirm(
        `Use the recommended ${p.label} defaults?`,
        tierLines(cat.defaults).join("\n") + "\n\nChoose 'no' to pick each tier yourself.");
      let tiers = cat.defaults;
      if (!useDefaults) {
        const chosen: Record<string, string> = {};
        for (const t of TIERS) {
          const m = await chooseTierModel(p.key, t, ctx.ui);
          if (m === undefined) return;
          chosen[t] = m;
        }
        tiers = chosen;
      }
      await applyAndVerify(ctx.ui, { provider: p.key, tiers });
    },
  });

  pi.registerCommand("fleet-provider", {
    description: "Show providers and key status; `<provider> <key>` sets a key",
    handler: async (args, ctx) => {
      const [name, ...rest] = (args ?? "").trim().split(/\s+/).filter(Boolean);
      if (name && rest.length) {
        try {
          const r = await fleetdSend<{ masked: string; env_var: string; note: string }>(
            "POST", `/providers/${name}/key`, { api_key: rest.join(" ") });
          ctx.ui.notify(`saved ${r.env_var} = ${r.masked} — ${r.note}`, "info");
        } catch (err) {
          ctx.ui.notify(`could not save key: ${err}`, "error");
        }
        return;
      }
      try {
        const resp = await fleetdGet<ProvidersResp>("/providers");
        ctx.ui.setWidget("dnc-setup", [
          `Providers (current: ${resp.current})`,
          ...resp.providers.flatMap((p) => [
            `  ${p.key_present ? "🔑" : "  "} ${p.label.padEnd(12)} ${p.key_env}` +
            `${p.has_closed_models ? "" : "  [open weights only]"}`,
            `       ${p.note}`,
            `       key: ${p.signup}`,
          ]),
          "",
          "Set one with:  /fleet-provider <provider> <api-key>   (or run /fleet-setup)",
        ]);
      } catch (err) {
        ctx.ui.notify(`fleetd unreachable: ${err}`, "error");
      }
    },
  });

  pi.registerCommand("fleet-model", {
    description: "Show or change which model backs a tier — `/fleet-model s2 <model>`",
    getArgumentCompletions: (prefix) => {
      const items = TIERS.filter((t) => t.startsWith(prefix)).map((t) => ({ value: t, label: `tier:${t}` }));
      return items.length ? items : null;
    },
    handler: async (args, ctx) => {
      const [tier, ...rest] = (args ?? "").trim().split(/\s+/).filter(Boolean);
      let current: TiersResp;
      try {
        current = await fleetdGet<TiersResp>("/tiers");
      } catch (err) {
        ctx.ui.notify(`fleetd unreachable: ${err}`, "error");
        return;
      }
      if (!tier) {
        ctx.ui.setWidget("dnc-setup", [
          `Tiers (provider ${current.provider}${current.key_present ? "" : " — ⚠️ no API key set"})`,
          ...TIERS.map((t) => `  ${t.toUpperCase()}  ${current.tiers[t] ?? "(unset)"}   ${TIER_JOB[t]}`),
          "", "Change one:  /fleet-model s2 <model>   ·   browse:  /fleet-model s2",
        ]);
        return;
      }
      if (!(TIERS as readonly string[]).includes(tier)) {
        ctx.ui.notify(`unknown tier ${tier} — use one of ${TIERS.join("|")}`, "error");
        return;
      }
      const model = rest.length ? rest.join(" ") : await chooseTierModel(current.provider, tier, ctx.ui);
      if (!model) return;
      await applyAndVerify(ctx.ui, { tiers: { [tier]: model } });
    },
  });

  pi.registerCommand("fleet-verify", {
    description: "Probe every tier with a 1-token completion to prove routing works",
    handler: async (_args, ctx) => {
      ctx.ui.setWidget("dnc-setup", ["Probing each tier…"]);
      try {
        const v = await fleetdSend<VerifyResp>("POST", "/tiers/verify");
        ctx.ui.setWidget("dnc-setup", verifyLines(v));
      } catch (err) {
        ctx.ui.notify(`verify failed: ${err}`, "error");
      }
    },
  });
}
