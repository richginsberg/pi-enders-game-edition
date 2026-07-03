/**
 * Fleet fan-out scheduler: decompose work into subtasks, size each by
 * complexity × context, dispatch in parallel across squads via tier virtual
 * models. Extends Pi's subagent example (process-per-agent, isolated context).
 */

export interface Subtask {
  id: string;
  prompt: string;
  complexity: "low" | "medium" | "high" | "max";
  estCtxTokens: number;
}

/** Squad-aware concurrency: S3 is wide but slow; S0 is metered by cost. */
export const MAX_INFLIGHT: Record<string, number> = {
  s0: 2,
  s1: 2,
  s2: 6,
  s3: 24,
};

export async function dispatch(_subtasks: Subtask[]): Promise<Map<string, string>> {
  // TODO(M1):
  // - map complexity → tier model (tier:auto with x-dnc-complexity header)
  // - spawn `pi --mode json` children under per-squad concurrency limits
  // - collect final-message outputs keyed by subtask id
  throw new Error("TODO(M1)");
}
