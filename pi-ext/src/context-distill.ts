/**
 * Salience-judge write path (client side).
 *
 * At the end of an agent response (`agent_end`), send the new slice of the
 * conversation to the context sidecar's `/distill`, where a cheap S3 judge extracts
 * durable facts and writes them to the repo's pgvector partition. We debounce so
 * trivial turns don't trigger a judge call — only distill once enough new content
 * has accrued (approximating "milestone / session end", since Pi has no session_end
 * hook). `/remember` forces a distill of everything new on demand.
 *
 * `renderTranscript` and `enoughToDistill` are pure (unit-testable); the hook does I/O.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const MIN_NEW_CHARS = 600; // below this, keep accumulating rather than calling the judge

interface MsgLike {
  role?: string;
  content?: unknown;
}

/** Flatten a Pi message's content (string or content-part array) to plain text. */
function messageText(msg: MsgLike): string {
  const c = msg.content;
  if (typeof c === "string") return c;
  if (Array.isArray(c)) {
    return c
      .map((part) => (typeof part === "string" ? part : typeof (part as { text?: string }).text === "string" ? (part as { text: string }).text : ""))
      .join(" ");
  }
  return "";
}

/** Render a slice of messages to a role-tagged transcript for the judge. */
export function renderTranscript(messages: MsgLike[]): string {
  return messages
    .map((m) => {
      const text = messageText(m).trim();
      return text ? `${(m.role ?? "unknown").toUpperCase()}: ${text}` : "";
    })
    .filter(Boolean)
    .join("\n\n");
}

/** Debounce gate: is the new transcript substantive enough to distill? */
export function enoughToDistill(transcript: string, force = false): boolean {
  return force ? transcript.trim().length > 0 : transcript.length >= MIN_NEW_CHARS;
}

export function registerContextDistill(pi: ExtensionAPI): void {
  let distilledCount = 0; // messages already sent to the judge (watermark)
  let latest: MsgLike[] = []; // freshest full conversation, captured at agent_end

  async function distill(force: boolean): Promise<number | null> {
    const fresh = latest.slice(distilledCount);
    const transcript = renderTranscript(fresh);
    if (!enoughToDistill(transcript, force)) return null;
    try {
      const { contextPost } = await import("./config.js");
      const res = await contextPost<{ facts: number; added: number }>("/distill", {
        transcript,
        cwd: process.cwd(),
        provenance: { source: "pi-session", ts: String(Math.floor(Date.now() / 1000)) },
      });
      distilledCount = latest.length; // advance the watermark only on a successful call
      return res.added;
    } catch {
      return null; // sidecar down: leave the watermark, retry after the next turn
    }
  }

  pi.on("agent_end", async (event) => {
    latest = (event.messages as unknown as MsgLike[]) ?? [];
    await distill(false);
  });

  pi.registerCommand("remember", {
    description: "Distill the session so far into long-term context now (salience judge)",
    handler: async (_args, ctx) => {
      const added = await distill(true);
      if (added === null) ctx.ui.notify("context: nothing new to remember (or sidecar down)", "info");
      else ctx.ui.notify(`context: remembered ${added} durable fact${added === 1 ? "" : "s"}`, "info");
    },
  });
}
