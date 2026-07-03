/**
 * Divide-and-conquer Pi extension entry point.
 * Async factory: fleet models are registered before startup completes.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { registerFleetCommand } from "./fleet-command.js";
import { registerFleetProvider } from "./provider.js";
import { registerTasksCommand } from "./tasks-command.js";

export default async function (pi: ExtensionAPI) {
  await registerFleetProvider(pi);
  registerFleetCommand(pi);
  registerTasksCommand(pi);

  // TODO(M3): /deploy wizard (ctx.ui.custom() multi-step form → fleetd play).
  // TODO(M1): before_provider_request hook to attach x-dnc-complexity / x-dnc-ctx
  //           hints when the active model is a tier:* virtual model.
}
