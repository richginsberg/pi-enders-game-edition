/**
 * The relentless outer loop ("Ralph loop"): re-run the implementer agent against
 * unmet DoD items until everything verifies, budget is exhausted, or a human
 * escalation is required. Each iteration gets a FRESH context — the ledger (via
 * fleetd /tasks) carries state between iterations, not the context window.
 *
 * Implementer agents are `pi --mode json -p "<prompt>"` child processes running
 * inside a tmux session (so /tasks peek can watch them), with the model set to a
 * fleet tier virtual model.
 */
import type { DodItem, TaskSpec } from "./dod.js";

export interface IterationResult {
  iteration: number;
  unmetBefore: number;
  unmetAfter: number;
  transcriptPath: string;
}

export async function runRalphLoop(task: TaskSpec): Promise<void> {
  for (let i = 0; i < task.maxIterations; i++) {
    const unmet = task.dod.filter((d) => !d.done);
    if (unmet.length === 0) return; // done — the only clean exit

    // TODO(M2):
    // 1. Build iteration prompt: task.prompt + ledger summary + unmet DoD items.
    // 2. Spawn implementer in tmux: pi --mode json, model = tier:<task.tier>.
    // 3. Verify each unmet item (runCheck below); judge-verified items use a
    //    DIFFERENT model than the implementer.
    // 4. PUT progress to fleetd /tasks/<id> (iteration, done_items, engaged hosts).
    // 5. No progress two iterations in a row → escalate tier once, then mark
    //    status=escalated and stop (human review), never silent-fail.
    throw new Error("TODO(M2)");
  }
}

export async function runCheck(_item: DodItem, _cwd: string): Promise<boolean> {
  // command checks: pi.exec / child_process with timeout; judge checks: fleet
  // model with rubric, majority vote across 3 cheap S3 judges + 1 S1 tiebreak.
  throw new Error("TODO(M2)");
}
