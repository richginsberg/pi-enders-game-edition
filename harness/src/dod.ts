/** Definition-of-done: materialization and verification. */

export interface DodItem {
  description: string;
  /** How to verify: a shell command (exit 0 = pass) or "judge" for LLM verification. */
  check: { kind: "command"; command: string } | { kind: "judge"; rubric: string };
  done: boolean;
}

export interface TaskSpec {
  id: string;
  title: string;
  prompt: string;
  cwd: string;
  dod: DodItem[];
  maxIterations: number;
  /** Tier for the implementer agent; judge always runs on a different model. */
  tier: "auto" | "s0" | "s1" | "s2" | "s3";
}

/**
 * Ask a fleet model to turn a raw task prompt into a concrete DoD checklist
 * (tests pass, lint clean, acceptance criteria...). M2 scope.
 */
export async function materializeDod(_prompt: string): Promise<DodItem[]> {
  throw new Error("TODO(M2): DoD materialization via tier:s1 model");
}
