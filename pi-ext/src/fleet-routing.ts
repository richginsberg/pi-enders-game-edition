/**
 * Fleet routing ledger + `/fleet-routing` command.
 *
 * The orchestrator can only report which node served each subagent if that fact reaches
 * it — but the routing headers (x-litellm-model-api-base / x-dnc-squad) live on each
 * subagent's own provider responses, which the orchestrator never sees (it only gets the
 * subagent's final text). So a fan-out report shows "N/A" for the node column.
 *
 * This records every fleet-served response — keyed by the SESSION that made the call
 * (ctx.sessionManager.getSessionFile(), which is the subagent's own session for a fork) —
 * to a per-project JSONL ledger. `/fleet-routing` then shows the per-session → node
 * attribution + a node histogram, so after a fan-out you can see exactly which BC-250
 * served each worker. The orchestrator can also be told to read the ledger to fill its
 * own table.
 */
import fs from "node:fs";
import path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export interface RoutingEntry {
  ts: number;
  session: string; // subagent session basename, or "main"
  tier: string;
  node: string;
}

export function ledgerPath(cwd: string): string {
  return path.join(cwd, ".pi-subagents", "dnc-routing.jsonl");
}

/** Append one routing record. Best-effort: never throw into the provider hook. */
export function recordRouting(cwd: string, entry: RoutingEntry): void {
  try {
    const p = ledgerPath(cwd);
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.appendFileSync(p, JSON.stringify(entry) + "\n");
  } catch {
    /* ledger is diagnostic only */
  }
}

/** Reduce a session file path (or null) to a short, worker-identifying label. */
export function sessionLabel(sessionFile: string | null | undefined): string {
  if (!sessionFile) return "main";
  const base = path.basename(sessionFile).replace(/\.jsonl$/, "");
  // subagent transcripts look like "<id>_worker_3_transcript" — keep the telling part
  const m = base.match(/_(worker|delegate|planner|reviewer|scout|oracle|researcher)_(\d+)/);
  return m ? `${m[1]}_${m[2]}` : base.slice(0, 24);
}

/** Pure: fold ledger entries into a human summary (per-session node + node histogram). */
export function summarizeRouting(entries: RoutingEntry[]): string[] {
  if (entries.length === 0) return ["fleet routing: no calls recorded yet"];
  const bySession = new Map<string, Map<string, number>>();
  const byNode = new Map<string, number>();
  for (const e of entries) {
    byNode.set(e.node, (byNode.get(e.node) ?? 0) + 1);
    const s = bySession.get(e.session) ?? new Map<string, number>();
    s.set(`${e.tier} · ${e.node}`, (s.get(`${e.tier} · ${e.node}`) ?? 0) + 1);
    bySession.set(e.session, s);
  }
  const lines = [`fleet routing: ${entries.length} calls, ${bySession.size} session(s), ${byNode.size} node(s)`];
  lines.push("by node:");
  for (const [node, n] of [...byNode.entries()].sort((a, b) => b[1] - a[1])) {
    lines.push(`  ${node}  ×${n}`);
  }
  lines.push("by session:");
  for (const [session, hits] of bySession) {
    const where = [...hits.entries()].sort((a, b) => b[1] - a[1]).map(([k]) => k).join(", ");
    lines.push(`  ${session}: ${where}`);
  }
  return lines;
}

export function registerFleetRouting(pi: ExtensionAPI): void {
  pi.registerCommand("fleet-routing", {
    description: "Show which fleet node served each subagent (from the routing ledger); `clear` to reset",
    handler: async (args, ctx) => {
      const p = ledgerPath(process.cwd());
      if ((args ?? "").trim() === "clear") {
        try {
          fs.rmSync(p);
        } catch {
          /* nothing to clear */
        }
        ctx.ui.notify("fleet routing ledger cleared", "info");
        return;
      }
      let entries: RoutingEntry[] = [];
      try {
        entries = fs
          .readFileSync(p, "utf8")
          .split("\n")
          .filter(Boolean)
          .map((l) => JSON.parse(l) as RoutingEntry);
      } catch {
        /* no ledger yet */
      }
      ctx.ui.setWidget("dnc-routing", summarizeRouting(entries));
    },
  });
}
