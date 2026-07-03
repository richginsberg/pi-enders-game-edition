import assert from "node:assert/strict";
import { test } from "node:test";

const { isVague, buildContextBlock } = await import("../src/context-inject.ts");

test("isVague flags short and under-specified prompts", () => {
  assert.equal(isVague("fix the bug"), true);
  assert.equal(isVague("make it faster"), true);
  assert.equal(isVague("clean this up please"), true);
});

test("isVague passes specific prompts through", () => {
  // has a file path
  assert.equal(isVague("update the retry logic in src/store.py to back off exponentially"), false);
  // has code punctuation / identifier
  assert.equal(isVague("why does parse_process_line() drop the last --flag=value token"), false);
  // long and detailed even without punctuation
  assert.equal(
    isVague("we should rework the router so that requests are grouped by their prompt prefix and sent to the same host"),
    false,
  );
});

test("isVague ignores empty input", () => {
  assert.equal(isVague("   "), false);
});

test("buildContextBlock drops weak matches and formats the rest", () => {
  const block = buildContextBlock([
    { kind: "decision", text: "chose pgvector over sqlite-vec", score: 0.82 },
    { kind: "constraint", text: "no committed local settings", score: 0.55 },
    { kind: "outcome", text: "irrelevant noise", score: 0.12 }, // below MIN_SCORE
  ]);
  assert.match(block, /Relevant prior context/);
  assert.match(block, /- \[decision\] chose pgvector/);
  assert.match(block, /- \[constraint\] no committed local settings/);
  assert.doesNotMatch(block, /irrelevant noise/);
});

test("buildContextBlock returns empty string when nothing clears the threshold", () => {
  assert.equal(buildContextBlock([{ kind: "outcome", text: "weak", score: 0.1 }]), "");
  assert.equal(buildContextBlock([]), "");
});

test("buildContextBlock truncates long text", () => {
  const long = "x".repeat(500);
  const block = buildContextBlock([{ kind: "decision", text: long, score: 0.9 }]);
  assert.ok(block.includes("…"));
  assert.ok(block.length < 500 + 100);
});
