#!/usr/bin/env -S npx tsx
/** dnc-run: run a task relentlessly. `dnc-run "<task prompt>" [--tier auto]` */
import { materializeDod } from "./dod.js";
import { runRalphLoop } from "./ralph.js";

const prompt = process.argv[2];
if (!prompt) {
  console.error("usage: dnc-run \"<task prompt>\" [--tier auto|s0|s1|s2|s3]");
  process.exit(1);
}

const tier = (process.argv.includes("--tier")
  ? process.argv[process.argv.indexOf("--tier") + 1]
  : "auto") as "auto" | "s0" | "s1" | "s2" | "s3";

const dod = await materializeDod(prompt);
await runRalphLoop({
  id: `task-${Date.now().toString(36)}`,
  title: prompt.slice(0, 60),
  prompt,
  cwd: process.cwd(),
  dod,
  maxIterations: 20,
  tier,
});
