# Example `IDEA.md` seeds

Ready-to-run seeds for the [team fan-out prompt](../team-fanout-prompt.md). Each is written
in the voice of a **customer or stakeholder** — it says what they want and why, and
deliberately *avoids* prescribing files, tech, or API design. That gap is the point: the
`product-manager` and `principal-engineer` (Architect) personas turn the brief into real
requirements and architecture in Wave 1, and the rest of the team builds off *their* plan.

## How to use one

```bash
# pick a seed, copy it into the target repo's root as IDEA.md, then run the fan-out prompt
cp docs/ideas/zero-to-one.example.md ~/code/testrepo/IDEA.md
```

Then paste the v3 team fan-out prompt into Pi (see [team-fanout-prompt.md](../team-fanout-prompt.md)).

## The seeds — one per common situation

| File | Situation | Stakeholder voice |
|---|---|---|
| [zero-to-one.example.md](zero-to-one.example.md) | Brand-new product from nothing | A neighbor who wants a tool-sharing app |
| [iterate-mature.example.md](iterate-mature.example.md) | Extend a mature product | Head of CS hearing recurring feature asks |
| [tech-debt.example.md](tech-debt.example.md) | Pay down debt (symptoms, not refactors) | A PM whose team ships slower every quarter |
| [bug-fix.example.md](bug-fix.example.md) | Fix broken behavior in production | A shop owner losing orders at checkout |
| [port-language.example.md](port-language.example.md) | Re-platform to a new stack | An eng director who can't hire for the old one |
| [accessibility-compliance.example.md](accessibility-compliance.example.md) | Meet a legal/compliance bar | A program manager under an accessibility mandate |
| [performance-scale.example.md](performance-scale.example.md) | Hold up under growth | A VP fielding "it's slow" complaints |

## Writing your own
Copy the template at the bottom of [team-fanout-prompt.md](../team-fanout-prompt.md) and
rewrite it in your own words. Two rules keep the fan-out honest:
- **Describe the outcome, not the implementation.** "I want to see who's blocked at a
  glance," not "add a `/status` endpoint." Even for tech-debt, bug, and port seeds, lead
  with the *pain* and let the team diagnose the fix.
- **Make "what success looks like" concrete and observable.** Those lines become the
  acceptance criteria the Architect designs against and Wave 3 reviews the result on.
