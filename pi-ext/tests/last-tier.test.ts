import assert from "node:assert/strict";
import { test } from "node:test";

const { nodeLabel, formatLast, tierLabel } = await import("../src/last-tier.ts");

test("nodeLabel reduces an api_base to host:port", () => {
  assert.equal(nodeLabel("http://192.168.1.106:8080/v1"), "192.168.1.106:8080");
});

test("nodeLabel drops the port when absent and passes non-URLs through", () => {
  assert.equal(nodeLabel("https://api.z.ai/api/paas/v4"), "api.z.ai");
  assert.equal(nodeLabel("bare-host"), "bare-host");
  assert.equal(nodeLabel(undefined), undefined);
});

test("tierLabel prefers the gateway's resolved squad over the selected model", () => {
  assert.equal(tierLabel("s0", "tier:auto"), "tier:s0"); // auto resolved to s0 server-side
  assert.equal(tierLabel(undefined, "tier:s3"), "tier:s3"); // no header: explicit selection is exact
  assert.equal(tierLabel(undefined, undefined), "?");
});

test("formatLast shows the tier + node, appending a distinct deploy id", () => {
  assert.equal(
    formatLast("tier:s3", "192.168.1.106:8080", "s3-node-01"),
    "fleet last: tier:s3 · 192.168.1.106:8080 [s3-node-01]",
  );
});

test("formatLast omits the id tag when it duplicates the node label", () => {
  assert.equal(formatLast("tier:s1", "10.0.0.5:5000", "10.0.0.5:5000"), "fleet last: tier:s1 · 10.0.0.5:5000");
});

test("formatLast falls back gracefully when signals are missing", () => {
  assert.equal(formatLast("?", undefined, undefined), "fleet last: ? · ?");
  assert.equal(formatLast("tier:auto", undefined, "abc123"), "fleet last: tier:auto · abc123");
});
