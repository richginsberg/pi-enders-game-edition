# IDEA: We can't hire anyone to maintain our billing service, and it's on an island

## Who I am
I'm the Director of Engineering. One of our core backend services — the one that calculates
what each customer owes and hands it to billing — was written years ago in a language and
framework nobody on the current team knows well and that we can no longer hire for. I'm the
stakeholder here: I own the roadmap risk and the hiring budget, not the keyboard.

## The problem
This service works, but it's a liability. Every time it needs a change, we wait on one
contractor who still knows the old stack, and they're expensive and slow. Job posts for that
skill set get no qualified applicants. The rest of our backend is on a modern, mainstream
stack the whole team is fluent in — but this one service sits on an island, so it doesn't
share our libraries, our tooling, our CI, or our on-call runbooks. When it breaks at 2am,
the on-call engineer is looking at code in a language they've never written. It's the single
scariest dependency we have, and the risk grows every quarter we leave it.

## What I wish existed
I want this service rebuilt on the **same modern stack the rest of our backend uses**, so
any engineer on the team can own it, it shares our common tooling and deployment, and we can
actually hire for it. The rebuilt service has to do **exactly** what the current one does —
same inputs, same outputs, same money math — so nothing downstream notices the swap except
that we can finally maintain it. Getting the numbers even slightly wrong is charging
customers incorrectly, so behavior parity is non-negotiable.

## Who would use it
- **Our engineers** — who need to be able to read, change, test, and operate this service
  with the skills they already have.
- **The billing system and everything downstream** — which must keep receiving the same
  results in the same shape, with no disruption during or after the switch.
- **Whoever's on call** — who needs it to fit our existing runbooks and alerting.

## Constraints & must-haves
- **Behavior must match the existing service exactly.** Same calculations, same edge cases,
  same outputs. This has to be provable, not assumed — I want confidence the new one agrees
  with the old one before we trust it with real billing.
- Target the stack our team already runs (a mainstream, well-supported language and web
  framework — the same one our other services use). The whole point is to stop being exotic.
- We can't have a billing outage. There needs to be a safe way to switch over — ideally
  running the new one alongside the old and comparing — rather than a big-bang cutover.
- Bring it into our normal CI, tests, and deployment so it stops being a special snowflake.

## What success looks like
- The new service produces identical results to the old one across a broad set of real
  cases, and we can show that side-by-side comparison.
- We retire the old service and the contractor dependency entirely.
- A regular team engineer makes a change to it, through our normal pipeline, without anyone
  who knew the old stack involved.

## Out of scope (for now)
- New billing features or changing *what* we charge. This is a like-for-like re-platform,
  not a redesign — behavior changes would make it impossible to tell a port bug from a
  feature change.
- Rearchitecting how it fits into the wider system. Same responsibilities, new stack.
