# IDEA: We're losing orders at checkout and I can't figure out why

## Who I am
I run a small online shop — handmade ceramics. A few hundred orders a month on a good month.
I'm the owner, the packer, and the customer-support inbox. I am absolutely not technical; a
developer set up the store for me a while ago and I've been running it since.

## The problem
Customers are telling me they paid but never got a confirmation, and I've got no record of
their order. It doesn't happen every time — most checkouts are fine — but a few times a week
someone emails: "I was charged, where's my mug?" When I check, there's a payment in my
processor but no order in my system. So I'm refunding upset customers, or scrambling to
honor an order I can't see, and I look unprofessional either way. It seems to happen more
when it's busy — like during a sale — which is exactly when I can least afford it. I don't
know if it's the payment step, the phone-vs-laptop thing, or something else. I just know
**money comes out of the customer's account and no order shows up.**

## What I wish existed
I want every customer who successfully pays to reliably end up with an order I can see and
fulfill, and a confirmation they can trust — every single time, especially when it's busy.
And when something *does* go wrong, I want to not be flying blind: I need some way to tell
that it happened and to whom, instead of finding out from an angry email days later.

## Who would use it
- **My customers** — who need their payment to always turn into a real, confirmed order.
- **Me** — who needs to see every paid order and to be alerted (not surprised) if one ever
  fails to record, so I can make it right immediately.

## Constraints & must-haves
- Do not break the checkouts that already work. The store is my livelihood; a "fix" that
  makes normal orders fail is a catastrophe.
- No customer should ever be charged without getting an order. If we can't guarantee that,
  I'd rather the payment not go through than take someone's money for nothing.
- I need to understand, in plain language, what was going wrong and how I'll know it's
  fixed — not just "it's handled now."

## What success looks like
- The "I paid but got nothing" emails stop. I can go a full busy weekend — a sale — with
  zero mismatches between payments taken and orders recorded.
- If a checkout ever does fail after payment, I find out right away with enough detail to
  contact the customer and fix it, instead of discovering it by accident.

## Out of scope (for now)
- Redesigning checkout or adding new payment methods. I don't want it prettier or fancier.
  I want it to stop dropping orders.
- New store features of any kind. Just make the thing I have trustworthy.
