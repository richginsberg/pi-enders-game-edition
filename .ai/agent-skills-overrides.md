# agent-skills overrides — divide-and-conquer

Project-specific configuration layered on top of `@chankov/agent-skills`. This binds
each engineering persona to a **fleet squad tier** so persona subagents run on the
right class of hardware, and adds the two roles the package doesn't ship.

> Models are referenced as `fleet/<litellm-id>` — the `fleet` provider (see
> `pi-ext/src/provider.ts`) registers LiteLLM's `tier:*` virtual models with Pi.
> Any `fleet/tier:*` model triggers the tier-hint header injection
> (`pi-ext/src/tier-hints.ts`), so persona subagents route through the custom router
> exactly like interactive sessions.

## Persona → tier bindings

Rule of thumb: high-value / low-volume reasoning → largest models (S0/S1);
bulk implementation → complexity-routed `tier:auto`; cheap mechanical work → S3.

| Persona | Default model | Fallback | Why |
|---|---|---|---|
| **product-manager** *(added)* | `fleet/tier:s0` | `fleet/tier:s1` | Scoping/prioritization reasoning; low volume |
| **principal-engineer** *(added)* | `fleet/tier:s0` | `fleet/tier:s1` | System design + final sign-off on risky changes |
| **engineering-manager** *(added)* | `fleet/tier:s1` | `fleet/tier:s0` | Delivery coordination/sequencing; low volume, high leverage |
| **frontend-engineer** *(added)* | `fleet/tier:auto` | `fleet/tier:auto` | Bulk UI implementation; complexity-routed |
| **backend-engineer** *(added)* | `fleet/tier:auto` | `fleet/tier:auto` | Bulk server implementation; complexity-routed |
| **quality-assurance** *(added)* | `fleet/tier:auto` | `fleet/tier:auto` | Test strategy + acceptance; complements test-engineer |
| **build-release-manager** *(added)* | `fleet/tier:auto` | `fleet/tier:auto` | Release process/versioning; complements releaser |
| **platform-engineer** *(added)* | `fleet/tier:auto` | `fleet/tier:s1` | IaC/CI/tooling; escalates for hard infra design |
| **observability-engineer** *(added)* | `fleet/tier:auto` | `fleet/tier:auto` | Instrumentation, dashboards, alerts |
| **sre** *(added)* | `fleet/tier:auto` | `fleet/tier:s1` | Routine reliability mid-tier; hard incidents escalate |
| **designer** *(added)* | `fleet/tier:auto` | `fleet/tier:s1` | UX/interaction/visual design + accessibility |
| **ml-inference-engineer** *(added)* | `fleet/tier:s1` | `fleet/tier:s0` | Serving/quant/tuning — the fleet's core competency |
| **data-engineer** *(added)* | `fleet/tier:auto` | `fleet/tier:auto` | Pipelines, ETL, embedding ingestion into pgvector |
| **technical-writer** *(added)* | `fleet/tier:s3` | `fleet/tier:auto` | User-facing docs; complements inline `documenter` |
| architect | `fleet/tier:s1` | `fleet/tier:s0` | Structural design; heavy but frequent |
| planner | `fleet/tier:auto` | `fleet/tier:s1` | Complexity-routed; escalates when hard |
| builder | `fleet/tier:auto` | `fleet/tier:auto` | Bulk implementation; router sizes per subtask |
| test-engineer | `fleet/tier:auto` | `fleet/tier:auto` | Test authoring/running; mid tier |
| code-reviewer | `fleet/tier:auto` | `fleet/tier:s1` | Routine review; escalate high-stakes to Principal |
| security-auditor | `fleet/tier:s1` | `fleet/tier:s0` | High-stakes correctness; larger models |
| web-performance-auditor | `fleet/tier:auto` | — | Analysis, mid tier |
| documenter | `fleet/tier:s3` | `fleet/tier:auto` | Summarization; cheap/wide squad |
| releaser | `fleet/tier:auto` | — | Mechanical with judgment |
| researcher | `fleet/tier:auto` | `fleet/tier:s1` | Depth varies; complexity-routed |
| deep-researcher | `fleet/tier:s0` | `fleet/tier:s1` | Deepest synthesis; frontier |

Personas not listed (pi-exclusive: bowser, web-debugger, orchestrator) inherit the
agent-skills defaults; add rows here to bind them.

## Notes on tier choice
- **Explicit tiers (`tier:s0..s3`)** deterministically pin a persona to a squad,
  regardless of the per-request complexity heuristic. Use for roles whose value
  justifies a fixed class of model (PM, Principal, security).
- **`tier:auto`** lets the custom router pick the squad from the complexity/context
  headers. Use for roles whose difficulty varies per task (planner, builder,
  researcher). The harness fan-out sets `DNC_COMPLEXITY` per child; interactive
  runs default to `medium`.
- Switch a persona's model at runtime with `/agent-model <persona>` (agent-skills'
  harness reads the `models:` switch list).

## Caveat
The exact machine-readable override format lives in the package's
`docs/agent-skills-setup.md` (generated at `npx @chankov/agent-skills init`). If that
schema differs from this table, **this table is the source of truth for the bindings**
— reconcile the generated file to match it. The two added personas
(`agents/product-manager.md`, `agents/principal-engineer.md`) already carry their
bindings in-frontmatter and don't depend on this file.
