/**
 * Divide-and-conquer Pi extension entry point.
 * Async factory: fleet models are registered before startup completes.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { registerContextDistill } from "./context-distill.js";
import { registerContextInjection } from "./context-inject.js";
import { registerDeployCommand } from "./deploy-command.js";
import { registerFleetCommand } from "./fleet-command.js";
import { registerFleetProvider } from "./provider.js";
import { registerTasksCommand } from "./tasks-command.js";
import { registerTierHints } from "./tier-hints.js";

export default async function (pi: ExtensionAPI) {
  await registerFleetProvider(pi);
  registerFleetCommand(pi);
  registerTasksCommand(pi);
  registerTierHints(pi);
  registerDeployCommand(pi);
  registerContextInjection(pi);
  registerContextDistill(pi);
}
