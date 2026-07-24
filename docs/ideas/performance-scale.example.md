# IDEA: The app crawls exactly when we're winning, and it's costing us customers

## Who I am
I'm the VP of Product. We run a scheduling app for gyms and studios — members book classes,
staff manage the calendar. We've grown a lot this year, which is great, except our growth is
turning into our biggest problem. I'm the one getting the escalations and the churn reports.

## The problem
When it's busy, the app gets painfully slow. Monday mornings and the top of every hour —
right when a popular class opens for booking — pages take ten-plus seconds to load, buttons
spin, and members give up. One studio owner sent me a video of the booking page hanging while
their 6am class filled up, and told me three members left for a competitor over it. The worst
slowdowns happen at the exact moments that matter most to our customers. Support says it's
"load," engineering says it's "scale," and meanwhile the bigger and more successful a
customer is, the worse their experience — which is backwards. Our success is punishing our
best customers.

## What I wish existed
I want the app to stay **fast and responsive under load** — especially at the predictable
rush moments — so a member booking a popular class at 6am gets the same snappy experience as
someone browsing at 2pm. Booking a class should feel instant even when hundreds of people
are doing it at once. I want our largest studios to have our *best* performance, not our
worst. And I want us to know we can take the next wave of growth without this happening again.

## Who would use it
- **Members** booking classes — who need pages to load and bookings to go through quickly,
  even during a rush.
- **Studio staff** — who manage schedules and can't have the tool freeze during peak hours.
- **Our team** — who need to see where the slowness actually comes from, so we're fixing the
  real bottleneck instead of guessing.

## Constraints & must-haves
- Everything must keep working exactly as it does today — same bookings, same data, no double
  bookings, nothing lost. Faster, not different.
- No overselling a class. Whatever makes it fast under load must not let two people grab the
  last spot. Correctness under load matters as much as speed.
- I need to understand where the time is going, in plain terms, and see a measurable
  before/after — not just "we optimized it."
- It has to hold up at the peaks, not just on average. Fast-most-of-the-time is the problem
  we already have.

## What success looks like
- During a real Monday-morning peak, booking pages load quickly and bookings succeed, with no
  hangs — and we can show the response times dropped from ten-plus seconds to something that
  feels instant.
- Our biggest studios stop being our unhappiest, and the "it's slow" escalations dry up.
- We can point to headroom: evidence we can handle noticeably more load than we do today
  before anything degrades.

## Out of scope (for now)
- New features. Nobody's asking for more; they're asking for what exists to be fast.
- Redesigning the booking flow's look. Same screens, same steps — just fast when it counts.
