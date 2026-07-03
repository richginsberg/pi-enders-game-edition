/** /fleet — cluster status readout (M1: text summary; M4: full dashboard). */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { fleetdGet } from "./config.js";

interface HostRow {
  id: string;
  squad: string;
  gpu_arch: string | null;
  gpu_count: number;
  address: string;
}

interface DeploymentRow {
  id: string;
  host_id: string;
  server: string;
  model_id: string;
  status: string;
}

export function registerFleetCommand(pi: ExtensionAPI): void {
  pi.registerCommand("fleet", {
    description: "Show fleet status: hosts, squads, deployments",
    handler: async (_args, ctx) => {
      try {
        const [hosts, deps] = await Promise.all([
          fleetdGet<HostRow[]>("/hosts"),
          fleetdGet<DeploymentRow[]>("/deployments"),
        ]);
        const bySquad = new Map<string, HostRow[]>();
        for (const h of hosts) {
          bySquad.set(h.squad, [...(bySquad.get(h.squad) ?? []), h]);
        }
        const lines: string[] = [];
        for (const [squad, members] of [...bySquad.entries()].sort()) {
          lines.push(`${squad.toUpperCase()} — ${members.length} host(s)`);
          for (const h of members) {
            const hostDeps = deps.filter((d) => d.host_id === h.id);
            const running = hostDeps.map((d) => `${d.model_id} [${d.status}]`).join(", ") || "idle";
            lines.push(`  ${h.id} (${h.gpu_count}x ${h.gpu_arch ?? "api"}) ${running}`);
          }
        }
        ctx.ui.setWidget("dnc-fleet", lines.length ? lines : ["fleet: no hosts cataloged"]);
      } catch (err) {
        ctx.ui.notify(`fleetd unreachable: ${err}`, "error");
      }
    },
  });
}
