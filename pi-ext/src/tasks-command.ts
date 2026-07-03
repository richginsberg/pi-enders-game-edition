/** /tasks — dynamic checklist of harness tasks: DoD progress, engaged nodes, elapsed, tmux peek. */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { fleetdGet } from "./config.js";

interface TaskRow {
  id: string;
  title: string;
  definition_of_done: string[];
  done_items: boolean[];
  status: string;
  iteration: number;
  engaged_hosts: string[];
  engaged_models: string[];
  tmux_session: string | null;
  started_at: number;
}

function elapsed(startedAt: number): string {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - startedAt));
  return s < 3600 ? `${Math.floor(s / 60)}m${s % 60}s` : `${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m`;
}

export function registerTasksCommand(pi: ExtensionAPI): void {
  pi.registerCommand("tasks", {
    description: "Show harness task checklist; `/tasks peek <id>` opens the task's tmux view",
    handler: async (args, ctx) => {
      const [sub, taskId] = (args ?? "").trim().split(/\s+/);

      if (sub === "peek" && taskId) {
        const tasks = await fleetdGet<TaskRow[]>("/tasks");
        const task = tasks.find((t) => t.id === taskId);
        if (!task?.tmux_session) {
          ctx.ui.notify(`No tmux session for task ${taskId}`, "warning");
          return;
        }
        // Capture the pane rather than attaching — keeps Pi's TUI intact.
        const out = await pi.exec("tmux", ["capture-pane", "-pt", task.tmux_session, "-S", "-100"], {});
        ctx.ui.setWidget("dnc-peek", out.stdout.split("\n").slice(-30));
        return;
      }

      const tasks = await fleetdGet<TaskRow[]>("/tasks?status=running");
      if (tasks.length === 0) {
        ctx.ui.setWidget("dnc-tasks", ["no running tasks"]);
        return;
      }
      const lines: string[] = [];
      for (const t of tasks) {
        const done = t.done_items.filter(Boolean).length;
        lines.push(
          `${t.id} ${t.title} — ${done}/${t.definition_of_done.length} DoD, ` +
            `iter ${t.iteration}, ${elapsed(t.started_at)}, on ${t.engaged_hosts.join("+") || "?"}`,
        );
        t.definition_of_done.forEach((item, i) => {
          lines.push(`  [${t.done_items[i] ? "x" : " "}] ${item}`);
        });
      }
      ctx.ui.setWidget("dnc-tasks", lines);
    },
  });
}
