/**
 * Fleet fan-out scheduler: dispatch subtasks in parallel across squads as
 * `pi --mode json` child processes (isolated context each), with per-squad
 * concurrency limits. Complexity reaches the router via the DNC_COMPLEXITY env
 * var, which the pi-ext tier-hints extension turns into x-dnc-* headers.
 */
import { spawn } from "node:child_process";

export interface Subtask {
  id: string;
  prompt: string;
  complexity: "low" | "medium" | "high" | "max";
  /** Explicit squad; omit to let the router resolve tier:auto from complexity. */
  tier?: "s0" | "s1" | "s2" | "s3";
  cwd?: string;
}

export interface SubtaskResult {
  id: string;
  ok: boolean;
  /** Final assistant message text ("" if none). */
  output: string;
  exitCode: number;
  stderr: string;
  durationMs: number;
}

/** Squad-aware concurrency: S3 is wide but slow; S0 is metered by cost. */
export const MAX_INFLIGHT: Record<string, number> = {
  s0: 2,
  s1: 2,
  s2: 6,
  s3: 24,
};

// Complexity → squad, mirroring the router's pick_tier (used only for choosing
// which concurrency bucket a tier:auto subtask occupies).
const COMPLEXITY_SQUAD: Record<Subtask["complexity"], string> = {
  low: "s3",
  medium: "s2",
  high: "s1",
  max: "s0",
};

const piBin = () => process.env.DNC_PI_BIN ?? "pi";

class Semaphore {
  private queue: (() => void)[] = [];
  private inFlight = 0;
  private readonly limit: number;
  constructor(limit: number) {
    this.limit = limit;
  }

  async acquire(): Promise<void> {
    if (this.inFlight < this.limit) {
      this.inFlight++;
      return;
    }
    await new Promise<void>((resolve) => this.queue.push(resolve));
    this.inFlight++;
  }

  release(): void {
    this.inFlight--;
    this.queue.shift()?.();
  }
}

function runOne(task: Subtask, signal?: AbortSignal): Promise<SubtaskResult> {
  const model = task.tier ? `fleet/tier:${task.tier}` : "fleet/tier:auto";
  const args = ["--mode", "json", "-p", "--no-session", "--model", model, task.prompt];
  const start = Date.now();

  return new Promise((resolve) => {
    const proc = spawn(piBin(), args, {
      cwd: task.cwd ?? process.cwd(),
      shell: false,
      stdio: ["ignore", "pipe", "pipe"],
      env: { ...process.env, DNC_COMPLEXITY: task.complexity },
    });

    let buffer = "";
    let stderr = "";
    let lastAssistantText = "";

    const processLine = (line: string) => {
      if (!line.trim()) return;
      let event: { type?: string; message?: { role?: string; content?: unknown } };
      try {
        event = JSON.parse(line);
      } catch {
        return;
      }
      if (event.type === "message_end" && event.message?.role === "assistant") {
        const content = event.message.content;
        const text = Array.isArray(content)
          ? content
              .filter((c): c is { type: string; text: string } => (c as { type?: string }).type === "text")
              .map((c) => c.text)
              .join("\n")
          : typeof content === "string"
            ? content
            : "";
        if (text) lastAssistantText = text;
      }
    };

    proc.stdout.on("data", (data: Buffer) => {
      buffer += data.toString();
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) processLine(line);
    });
    proc.stderr.on("data", (data: Buffer) => {
      stderr += data.toString();
    });

    proc.on("close", (code) => {
      if (buffer.trim()) processLine(buffer);
      resolve({
        id: task.id,
        ok: (code ?? 1) === 0 && lastAssistantText !== "",
        output: lastAssistantText,
        exitCode: code ?? 1,
        stderr,
        durationMs: Date.now() - start,
      });
    });
    proc.on("error", (err) => {
      resolve({
        id: task.id,
        ok: false,
        output: "",
        exitCode: 1,
        stderr: String(err),
        durationMs: Date.now() - start,
      });
    });

    if (signal) {
      const kill = () => {
        proc.kill("SIGTERM");
        setTimeout(() => proc.kill("SIGKILL"), 5000).unref();
      };
      if (signal.aborted) kill();
      else signal.addEventListener("abort", kill, { once: true });
    }
  });
}

/** Dispatch all subtasks under per-squad concurrency limits; resolves when all finish. */
export async function dispatch(
  subtasks: Subtask[],
  signal?: AbortSignal,
): Promise<Map<string, SubtaskResult>> {
  const semaphores = new Map<string, Semaphore>(
    Object.entries(MAX_INFLIGHT).map(([squad, n]) => [squad, new Semaphore(n)]),
  );

  const results = await Promise.all(
    subtasks.map(async (task) => {
      const squad = task.tier ?? COMPLEXITY_SQUAD[task.complexity];
      const sem = semaphores.get(squad)!;
      await sem.acquire();
      try {
        return await runOne(task, signal);
      } finally {
        sem.release();
      }
    }),
  );
  return new Map(results.map((r) => [r.id, r]));
}
