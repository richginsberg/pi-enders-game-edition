# IDEA: Shipping anything to our internal analytics dashboard has become terrifying

## Who I am
I'm a product manager. I own the internal analytics dashboard the whole company uses to see
signups, revenue, and retention. I'm not an engineer, but I've been here long enough to feel
the difference between how this used to go and how it goes now.

## The problem
It didn't used to be like this. Two years ago I could ask for a small change — "add a filter
for region" — and it'd land the same week. Now the same size of change takes a month, and
half the time it breaks something unrelated. Last sprint we added one chart and the login
page went blank for a day. The engineers wince when I bring the dashboard up. Onboarding a
new engineer onto it takes forever because "only Priya really understands that part," and
Priya is leaving. Every estimate comes with a sharp intake of breath. I don't know what's
under the hood, but I know the symptoms: **slow, fragile, and scary to touch.**

## What I wish existed
I want changes to this dashboard to be **safe and boring again.** A small change should be a
small effort with a predictable outcome, and it shouldn't be able to take down something
unrelated. I want a new engineer to be able to work on it without a three-week apprenticeship
and without fear. I don't care what the fix is called — I care that we get our velocity and
our confidence back.

## Who would use it
- **The engineers** who have to work in this code and currently dread it.
- **Me and the other PMs** who depend on being able to request changes and get reliable
  estimates.
- **Everyone in the company** who reads the dashboard and needs it to not break.

## Constraints & must-haves
- The dashboard has to keep working the entire time. I can't tell the CEO "analytics is
  down for two weeks while we clean up." Whatever we do has to be incremental.
- No feature regressions. Users shouldn't notice anything except that it stops breaking.
- I need to be able to explain to leadership what we're getting for the time we spend — so
  the work should come with a clear before/after story, not just "we refactored things."

## What success looks like
- A change that used to take a month and cause a fire takes days and doesn't. We can point
  to a specific recent change that went in cleanly as proof.
- A newly-onboarded engineer makes a real change to it in their first week.
- The team stops flinching when the dashboard comes up in planning.

## Out of scope (for now)
- New dashboard features. This is explicitly about making the existing thing sound and
  safe — not adding to it while it's shaky.
- A ground-up rewrite. I've been burned by "let's just rebuild it" before; if that's truly
  the answer I need to hear *why*, with the risks, not have it assumed.
