/**
 * Tier-hint injection: when the active model is a fleet tier:* virtual model,
 * attach x-dnc-* headers so the LiteLLM custom router can tier-select and the
 * affinity layer can see context size without re-estimating.
 *
 * Complexity comes from session state: subagent spawners (harness fan-out) set
 * it per child via the DNC_COMPLEXITY env var; interactive sessions default to
 * "medium" and can override with /complexity.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const COMPLEXITIES = ["low", "medium", "high", "max"] as const;
type Complexity = (typeof COMPLEXITIES)[number];

export function registerTierHints(pi: ExtensionAPI): void {
  let complexity: Complexity =
    (COMPLEXITIES as readonly string[]).includes(process.env.DNC_COMPLEXITY ?? "")
      ? (process.env.DNC_COMPLEXITY as Complexity)
      : "medium";

  pi.registerCommand("complexity", {
    description: "Set routing complexity hint (low|medium|high|max) for tier:auto",
    getArgumentCompletions: (prefix) => {
      const items = COMPLEXITIES.filter((c) => c.startsWith(prefix)).map((c) => ({
        value: c,
        label: c,
      }));
      return items.length ? items : null;
    },
    handler: async (args, ctx) => {
      const v = (args ?? "").trim();
      if ((COMPLEXITIES as readonly string[]).includes(v)) {
        complexity = v as Complexity;
        ctx.ui.setStatus("dnc-complexity", `complexity: ${complexity}`);
      } else {
        ctx.ui.notify(`complexity is "${complexity}" — pass one of ${COMPLEXITIES.join("|")}`, "info");
      }
    },
  });

  pi.on("before_provider_request", (event, ctx) => {
    const model = ctx.model;
    if (!model || model.provider !== "fleet" || !model.id.startsWith("tier:")) return;

    const usage = ctx.getContextUsage();
    const payload = event.payload as Record<string, unknown>;
    // LiteLLM forwards extra_headers to the proxy request; the custom router
    // reads them from proxy_server_request.headers.
    return {
      ...payload,
      extra_headers: {
        ...(payload.extra_headers as Record<string, string> | undefined),
        "x-dnc-complexity": complexity,
        "x-dnc-ctx": String(usage?.tokens ?? 0),
      },
    };
  });
}
