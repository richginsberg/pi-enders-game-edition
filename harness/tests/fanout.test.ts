import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

process.env.DNC_PI_BIN = join(import.meta.dirname, "mock-pi.sh");

const { dispatch } = await import("../src/fanout.ts");

test("dispatch collects final assistant output per subtask", async () => {
  const results = await dispatch([
    { id: "a", prompt: "task-a", complexity: "low" },
    { id: "b", prompt: "task-b", complexity: "high", tier: "s1" },
  ]);
  assert.equal(results.size, 2);
  const a = results.get("a")!;
  assert.ok(a.ok);
  assert.match(a.output, /done:task-a/);
  assert.match(a.output, /model=fleet\/tier:auto/);
  assert.match(a.output, /cx=low/); // DNC_COMPLEXITY reached the child
  assert.match(results.get("b")!.output, /model=fleet\/tier:s1/);
});

test("per-squad concurrency cap is respected", async () => {
  const dir = mkdtempSync(join(tmpdir(), "dnc-conc-"));
  const inflight = mkdtempSync(join(tmpdir(), "dnc-inflight-"));
  process.env.MOCK_CONCURRENCY_DIR = inflight;
  process.env.MOCK_SLEEP = "0.3";
  try {
    // 6 "max" (s0) subtasks, cap is 2
    const tasks = Array.from({ length: 6 }, (_, i) => ({
      id: `t${i}`,
      prompt: `p${i}`,
      complexity: "max" as const,
    }));
    const results = await dispatch(tasks);
    assert.equal([...results.values()].filter((r) => r.ok).length, 6);
    const peaks = readFileSync(join(inflight, "..", "peak.log"), "utf8")
      .trim()
      .split("\n")
      .map(Number);
    assert.ok(Math.max(...peaks) <= 2, `peak concurrency ${Math.max(...peaks)} exceeds cap 2`);
  } finally {
    delete process.env.MOCK_CONCURRENCY_DIR;
    delete process.env.MOCK_SLEEP;
    rmSync(dir, { recursive: true, force: true });
    rmSync(inflight, { recursive: true, force: true });
  }
});

test("failed child yields ok=false, not a rejection", async () => {
  process.env.DNC_PI_BIN = "/nonexistent/pi-binary";
  try {
    const results = await dispatch([{ id: "x", prompt: "p", complexity: "medium" }]);
    const x = results.get("x")!;
    assert.equal(x.ok, false);
    assert.equal(x.output, "");
  } finally {
    process.env.DNC_PI_BIN = join(import.meta.dirname, "mock-pi.sh");
  }
});
