/**
 * Divide-and-conquer Pi extension entry point.
 * Async factory: fleet models are registered before startup completes.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { registerFleetCommand } from "./fleet-command.js";
import { registerFleetProvider } from "./provider.js";
import { registerTasksCommand } from "./tasks-command.js";
import { registerTierHints } from "./tier-hints.js";

export default async function (pi: ExtensionAPI) {
  await registerFleetProvider(pi);
  registerFleetCommand(pi);
  registerTasksCommand(pi);
  registerTierHints(pi);

  // TODO(M3): /deploy wizard (ctx.ui.custom() multi-step form → fleetd play).
}
