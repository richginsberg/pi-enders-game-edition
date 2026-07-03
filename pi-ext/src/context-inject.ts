/**
 * Vague-prompt context injection.
 *
 * When a user prompt is short/under-specified, retrieve the most relevant prior
 * context for this repo from the pgvector store (via the context sidecar) and inject
 * it into the system prompt for that turn. A one-line status keeps it transparent:
 * the user sees that context was pulled, not silent prompt-stuffing.
 *
 * `isVague` and `buildContextBlock` are pure (unit-testable); the hook does the I/O.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

interface SearchHit {
  kind: string;
  text: string;
  score: number;
}

const MAX_ITEMS = 5;
const MIN_SCORE = 0.3; // drop weak matches so we don't inject noise
const MAX_TEXT = 300;

// Signals that a prompt is already specific: paths, code punctuation, file extensions,
// snake/camelCase identifiers, URLs, quoted strings, issue refs.
const SPECIFIC = /[/`(){}]|\.[a-z]{1,4}\b|[a-z]+_[a-z]+|[a-z][A-Z]|https?:\/\/|"[^"]+"|#\d+/;

/** Heuristic: is this prompt vague enough to benefit from injected context? */
export function isVague(prompt: string): boolean {
  const words = prompt.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return false;
  if (words.length <= 6) return true; // "fix the bug", "make it faster"
  if (words.length < 15 && !SPECIFIC.test(prompt)) return true;
  return false;
}

/** Format retrieved hits as a system-prompt appendix. Returns "" if nothing usable. */
export function buildContextBlock(hits: SearchHit[]): string {
  const useful = hits
    .filter((h) => h.score >= MIN_SCORE)
    .slice(0, MAX_ITEMS)
    .map((h) => {
      const text = h.text.length > MAX_TEXT ? `${h.text.slice(0, MAX_TEXT)}…` : h.text;
      return `- [${h.kind}] ${text.replace(/\s+/g, " ").trim()}`;
    });
  if (useful.length === 0) return "";
  return [
    "## Relevant prior context (divide-and-conquer long-term memory)",
    "The request is brief; these facts from this repo's history may be relevant.",
    "Use them if applicable, ignore them if not.",
    "",
    ...useful,
  ].join("\n");
}

export function registerContextInjection(pi: ExtensionAPI): void {
  pi.on("before_agent_start", async (event, ctx) => {
    const prompt = event.prompt ?? "";
    if (!isVague(prompt)) return;

    let hits: SearchHit[];
    try {
      // Dynamic import keeps this module's pure helpers importable without the
      // config/runtime deps (for unit tests); Pi's loader resolves it at runtime.
      const { contextPost } = await import("./config.js");
      hits = await contextPost<SearchHit[]>("/recall", { query: prompt, k: MAX_ITEMS, cwd: process.cwd() });
    } catch {
      return; // context sidecar down: proceed without injection, never block the turn
    }

    const block = buildContextBlock(hits);
    if (!block) return;

    const n = block.split("\n").filter((l) => l.startsWith("- [")).length;
    ctx.ui.setStatus("dnc-context", `pulled ${n} context item${n === 1 ? "" : "s"}`);
    return { systemPrompt: `${event.systemPrompt}\n\n${block}` };
  });
}
