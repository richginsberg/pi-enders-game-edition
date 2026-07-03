import assert from "node:assert/strict";
import { test } from "node:test";

const { renderTranscript, enoughToDistill } = await import("../src/context-distill.ts");

test("renderTranscript tags roles and flattens content-part arrays", () => {
  const t = renderTranscript([
    { role: "user", content: "make it faster" },
    { role: "assistant", content: [{ text: "I profiled" }, { text: "the loop" }] },
  ]);
  assert.equal(t, "USER: make it faster\n\nASSISTANT: I profiled the loop");
});

test("renderTranscript drops empty messages", () => {
  const t = renderTranscript([
    { role: "user", content: "" },
    { role: "assistant", content: "done" },
  ]);
  assert.equal(t, "ASSISTANT: done");
});

test("enoughToDistill debounces short transcripts", () => {
  assert.equal(enoughToDistill("short exchange"), false);
  assert.equal(enoughToDistill("x".repeat(600)), true);
});

test("enoughToDistill force ignores the length gate but not emptiness", () => {
  assert.equal(enoughToDistill("tiny", true), true);
  assert.equal(enoughToDistill("   ", true), false);
});
