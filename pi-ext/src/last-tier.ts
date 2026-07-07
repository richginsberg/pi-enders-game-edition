/**
 * "Last responded" footer: after each provider response, show which tier and
 * which fleet NODE actually served the turn. Answers the two operator questions
 * during a failover/fallback — "did my tier answer, or did it fall up?" and
 * "which box am I hitting?".
 *
 * The signal is LiteLLM's own response headers (normalized lowercase):
 *   x-litellm-model-api-base  ->  the node that served it (e.g. http://192.168.1.106:8080/v1)
 *   x-litellm-model-id        ->  the deployment id (our model_info.id, e.g. s3-node-01)
 * Header availability depends on transport; if absent we leave the last value up.
 *
 * Tier: for an explicit tier:sN selection ctx.model.id is exact. For tier:auto the
 * router resolves the squad server-side and does NOT echo it in a header today, so
 * we show the node (which reveals the squad to anyone who knows their fleet) and tag
 * the selection as "auto". A future router change could add an x-dnc-squad response
 * header to make the resolved tier exact here too.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

/** Reduce an api_base URL to host:port for a compact footer ("192.168.1.106:8080"). */
export function nodeLabel(apiBase: string | undefined): string | undefined {
  if (!apiBase) return undefined;
  try {
    const u = new URL(apiBase);
    return u.port ? `${u.hostname}:${u.port}` : u.hostname;
  } catch {
    return apiBase; // not a URL (some transports pass a bare host) — show as-is
  }
}

/** Build the footer string from the selected model id + resolved node/deployment. */
export function formatLast(modelId: string | undefined, node: string | undefined, deployId: string | undefined): string {
  const tier = modelId && modelId.startsWith("tier:") ? modelId : (modelId ?? "?");
  const where = node ?? deployId ?? "?";
  const id = deployId && deployId !== where ? ` [${deployId}]` : "";
  return `fleet last: ${tier} · ${where}${id}`;
}

export function registerLastTier(pi: ExtensionAPI): void {
  pi.on("after_provider_response", (event, ctx) => {
    // Only annotate fleet-served turns; leave other providers' footer untouched.
    if (ctx.model?.provider !== "fleet") return;
    const headers = (event.headers ?? {}) as Record<string, string>;
    const node = nodeLabel(headers["x-litellm-model-api-base"]);
    const deployId = headers["x-litellm-model-id"];
    if (!node && !deployId) return; // transport didn't surface headers — keep prior value
    ctx.ui.setStatus("dnc-last", formatLast(ctx.model?.id, node, deployId));
  });
}
