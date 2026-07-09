import assert from "node:assert/strict";
import { test } from "node:test";

const { summarizeRouting, sessionLabel, ledgerPath } = await import("../src/fleet-routing.ts");

test("sessionLabel extracts the worker identity from a transcript path", () => {
  assert.equal(sessionLabel("/x/.pi-subagents/f8fd8e87_worker_3_transcript.jsonl"), "worker_3");
  assert.equal(sessionLabel("/x/y/abc_delegate_0_transcript.jsonl"), "delegate_0");
  assert.equal(sessionLabel(null), "main");
  assert.equal(sessionLabel(undefined), "main");
});

test("ledgerPath is under the project .pi-subagents dir", () => {
  assert.equal(ledgerPath("/home/u/proj"), "/home/u/proj/.pi-subagents/dnc-routing.jsonl");
});

test("summarizeRouting groups by session and builds a node histogram", () => {
  const entries = [
    { ts: 1, session: "worker_0", tier: "tier:s3", node: "192.168.1.135:8080" },
    { ts: 2, session: "worker_1", tier: "tier:s3", node: "192.168.1.209:8080" },
    { ts: 3, session: "worker_0", tier: "tier:s3", node: "192.168.1.135:8080" },
  ];
  const out = summarizeRouting(entries).join("\n");
  assert.match(out, /3 calls, 2 session\(s\), 2 node\(s\)/);
  assert.match(out, /192\.168\.1\.135:8080  ×2/);
  assert.match(out, /worker_0: tier:s3 · 192\.168\.1\.135:8080/);
  assert.match(out, /worker_1: tier:s3 · 192\.168\.1\.209:8080/);
});

test("summarizeRouting handles an empty ledger", () => {
  assert.deepEqual(summarizeRouting([]), ["fleet routing: no calls recorded yet"]);
});
