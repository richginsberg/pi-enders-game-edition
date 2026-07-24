/**
 * `/fleet-power` — wake or shut down fleet tiers/nodes and watch them come up (or go down)
 * in real time. All the WoL / poweroff / health-poll logic lives in fleetd; this command
 * just parses the selector, confirms destructive OFFs, and renders fleetd's SSE progress
 * (per-node phase + elapsed + ETA) into a live widget.
 *
 *   /fleet-power s3 on        wake every S3 node, watch until serving
 *   /fleet-power s3 off       shut down S3 (confirms first; skips never_sleep nodes)
 *   /fleet-power all on
 *   /fleet-power 1,2,3 on     bc25001..bc25003
 *   /fleet-power .106 off     192.168.1.106
 *   /fleet-power s3 off force  also power off never_sleep nodes
 */
import type { ExtensionAPI, ExtensionUIContext } from "@earendil-works/pi-coding-agent";
import { fleetdGet, fleetdSend, fleetdStream } from "./config.js";

type Phase =
  | "waking" | "booting" | "loading" | "serving"
  | "stopping" | "offline" | "timeout" | "error";

interface NodeEvent {
  type: "node";
  name: string;
  ip: string | null;
  tier: string | null;
  phase: Phase;
  elapsed_s: number;
  eta_s: number;
  detail?: string;
}
interface PlanEvent {
  type: "plan";
  state: "on" | "off";
  budget_s: number;
  nodes: { name: string; ip: string | null; tier: string | null }[];
  skipped: { name: string; ip: string | null; reason: string }[];
  config: string | null;
}
interface SummaryEvent {
  type: "summary" | "done";
  state: "on" | "off";
  total: number;
  done: number;
  timeout: number;
  pending?: number;
  elapsed_s: number;
}
type PowerEvent = NodeEvent | PlanEvent | SummaryEvent;

interface Plan {
  state: "on" | "off";
  budget_s: number;
  nodes: { name: string; ip: string | null; tier: string | null }[];
  skipped: { name: string; ip: string | null; reason: string }[];
  config: string | null;
}

const ICON: Record<Phase, string> = {
  waking: "⏳", booting: "⏳", loading: "⏳", serving: "✅",
  stopping: "⏻", offline: "⭘", timeout: "⚠️", error: "✗",
};
const TIERS = new Set(["s0", "s1", "s2", "s3"]);

/** Parse "<selector> <on|off> [force]" into a fleetd query string. Selector is a tier,
 *  "all", a node list (1,2 / bc25005), or an ip list (.106 / 10.0.0.4). */
function parseArgs(raw: string): { query: string; state: "on" | "off"; force: boolean } | { error: string } {
  const toks = raw.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (toks.length === 0) return { error: "usage: /fleet-power <s3|all|1,2|.106> <on|off> [force]" };
  const state = toks.find((t) => t === "on" || t === "off") as "on" | "off" | undefined;
  if (!state) return { error: "specify on or off, e.g. /fleet-power s3 on" };
  const force = toks.includes("force");
  const sel = toks.find((t) => t !== "on" && t !== "off" && t !== "force");
  if (!sel) return { error: "specify what to power, e.g. /fleet-power s3 on" };

  const params = new URLSearchParams({ state });
  if (force) params.set("force", "true");
  if (sel === "all") params.set("all", "true");
  else if (TIERS.has(sel)) params.set("tier", sel);
  else if (/^[.\d]/.test(sel) && (sel.includes(".") || /^\.?\d+$/.test(sel)) && sel.split(",").every((s) => /^\.?[\d.]+$/.test(s)))
    params.set("ips", sel);
  else params.set("nodes", sel);
  return { query: params.toString(), state, force };
}

function nodeLine(e: NodeEvent): string {
  const where = `${e.name.padEnd(9)} ${(e.ip ?? "").padEnd(15)}`;
  const timing =
    e.phase === "serving" || e.phase === "offline"
      ? `${e.elapsed_s.toFixed(0)}s`
      : e.phase === "timeout" || e.phase === "error"
        ? (e.detail ?? "")
        : `${e.elapsed_s.toFixed(0)}s / ~${e.eta_s}s left`;
  return `  ${ICON[e.phase]} ${where} ${e.phase.padEnd(8)} ${timing}`;
}

interface NodeRow {
  name: string;
  ip: string | null;
  tier: string | null;
  never_sleep?: boolean;
  port?: number;
}

async function showNodes(ui: ExtensionUIContext): Promise<void> {
  const nodes = await fleetdGet<NodeRow[]>("/nodes");
  const lines = nodes.length
    ? nodes.map((n) => `  ${n.name.padEnd(9)} ${(n.ip ?? "").padEnd(15)} ${n.tier}${n.never_sleep ? "  (never_sleep)" : ""}`)
    : ["  (no nodes registered yet — /fleet-power register …)"];
  ui.setWidget("dnc-power", [`Fleet nodes (${nodes.length})`, ...lines]);
}

/** /fleet-power register <name> <ip> <mac> <tier> [never_sleep] [port=N] — prompts for any missing field. */
async function registerNode(toks: string[], ui: ExtensionUIContext): Promise<void> {
  let [name, ip, mac, tier] = toks;
  const never_sleep = toks.includes("never_sleep") || toks.includes("never-sleep");
  const portTok = toks.find((t) => t.startsWith("port="));
  const port = portTok ? Number(portTok.slice(5)) : undefined;

  name ||= (await ui.input("Node name", "bc25020")) ?? "";
  if (!name) return;
  ip ||= (await ui.input(`IP for ${name}`, "192.168.1.")) ?? "";
  if (!ip) return;
  mac ||= (await ui.input(`WoL MAC for ${name} (ONBOARD NIC)`, "aa:bb:cc:dd:ee:ff")) ?? "";
  if (!mac) return;
  tier ||= (await ui.select(`Tier for ${name}`, ["s3", "s2", "s1", "s0"])) ?? "";
  if (!tier) return;

  const body: Record<string, unknown> = { name, ip, mac, tier, never_sleep };
  if (port) body.port = port;
  try {
    await fleetdSend<NodeRow>("POST", "/nodes", body);
    ui.notify(`registered ${name} (${ip}, ${tier})`, "info");
  } catch (err) {
    const msg = String(err);
    if (msg.includes("already registered") &&
        (await ui.confirm(`${name} already exists`, "Replace its entry with these values?"))) {
      await fleetdSend<NodeRow>("POST", "/nodes", { ...body, overwrite: true });
      ui.notify(`replaced ${name} (${ip}, ${tier})`, "info");
    } else {
      ui.notify(`register failed: ${err}`, "error");
      return;
    }
  }
  await showNodes(ui);
}

/** /fleet-power deregister <name> — confirms, then removes it from the node file. */
async function deregisterNode(toks: string[], ui: ExtensionUIContext): Promise<void> {
  const name = toks[0] || (await ui.input("Node to de-register", "bc25020")) || "";
  if (!name) return;
  if (!(await ui.confirm(`De-register ${name}?`, "Removes it from the fleet node file (does not power it off)."))) return;
  try {
    await fleetdSend<NodeRow>("DELETE", `/nodes/${encodeURIComponent(name)}`);
    ui.notify(`de-registered ${name}`, "info");
    await showNodes(ui);
  } catch (err) {
    ui.notify(`de-register failed: ${err}`, "error");
  }
}

export function registerFleetPowerCommand(pi: ExtensionAPI): void {
  pi.registerCommand("fleet-power", {
    description: "Power fleet tiers/nodes on/off (watch them reach serving); register/deregister/list nodes",
    handler: async (args, ctx) => {
      const toks = String(args ?? "").trim().split(/\s+/).filter(Boolean);
      const sub = toks[0]?.toLowerCase();
      if (sub === "register" || sub === "add") return registerNode(toks.slice(1), ctx.ui);
      if (sub === "deregister" || sub === "remove") return deregisterNode(toks.slice(1), ctx.ui);
      if (sub === "list" || sub === "ls") return showNodes(ctx.ui);

      const parsed = parseArgs(String(args ?? ""));
      if ("error" in parsed) {
        ctx.ui.notify(parsed.error, "error");
        return;
      }
      const { query, state } = parsed;

      // 1. Dry-run plan first — show exactly what will be touched (and confirm OFF).
      let plan: Plan;
      try {
        plan = await fleetdGet<Plan>(`/power/plan?${query}`);
      } catch (err) {
        ctx.ui.notify(`fleetd power unavailable: ${err}`, "error");
        return;
      }
      if (plan.nodes.length === 0) {
        ctx.ui.notify("no matching nodes for that selector", "error");
        return;
      }
      const label = `Fleet power: ${plan.nodes.length} node(s) → ${state.toUpperCase()}`;
      const planLines = [
        `${label}  (budget ~${plan.budget_s}s, ${plan.config ?? "?"})`,
        ...plan.nodes.map((n) => `  • ${n.name.padEnd(9)} ${(n.ip ?? "").padEnd(15)} tier=${n.tier}`),
        ...plan.skipped.map((n) => `  – ${n.name.padEnd(9)} SKIPPED (${n.reason}; add 'force')`),
      ];
      ctx.ui.setWidget("dnc-power", planLines);

      if (state === "off") {
        const ok = await ctx.ui.confirm(
          `Power OFF ${plan.nodes.length} node(s)?`,
          "They'll be shut down via ssh poweroff and must be woken again.",
        );
        if (!ok) {
          ctx.ui.notify("power off aborted", "info");
          return;
        }
      }

      // 2. Fire + stream. Re-render the widget on every phase change / heartbeat.
      const nodes = new Map<string, NodeEvent>();
      let summary = "";
      const render = (footer: string) => {
        const rows = [...nodes.values()].sort((a, b) => a.name.localeCompare(b.name)).map(nodeLine);
        ctx.ui.setWidget("dnc-power", [label, ...rows, footer]);
      };
      render("starting…");

      try {
        for await (const ev of fleetdStream<PowerEvent>("GET", `/power/stream?${query}`)) {
          if (ev.type === "node") {
            nodes.set(ev.name, ev);
            render(summary || "running…");
          } else if (ev.type === "summary" || ev.type === "done") {
            const doneWord = state === "on" ? "serving" : "offline";
            summary =
              `  ${ev.done}/${ev.total} ${doneWord}` +
              (ev.timeout ? ` · ${ev.timeout} timed out` : "") +
              ` · ${ev.elapsed_s.toFixed(0)}s elapsed`;
            render(ev.type === "done" ? summary.trimStart() + " — done." : summary);
          }
        }
      } catch (err) {
        render(`stream error: ${err}`);
        return;
      }
      const settled = state === "on" ? "serving" : "offline";
      const good = [...nodes.values()].filter((n) => n.phase === settled).length;
      ctx.ui.notify(`fleet ${state}: ${good}/${nodes.size} ${settled}`, good === nodes.size ? "info" : "error");
    },
  });
}
