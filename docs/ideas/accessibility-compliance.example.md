# IDEA: Our benefits-application site has to be usable by everyone — and right now it isn't

## Who I am
I'm a program manager at a public agency. I'm responsible for the online form residents use
to apply for a benefit they're entitled to. I'm not an engineer. I answer to legal, to
leadership, and to the public — and lately to an advocacy group that filed a complaint.

## The problem
We got a formal complaint that our application site can't be used by people with
disabilities. A resident who is blind and uses a screen reader couldn't complete the form —
they got stuck and gave up, which means they couldn't apply for something they qualify for.
When we looked closer, it's broader than one person: someone who can't use a mouse can't get
through it with a keyboard, the text is too low-contrast for people with low vision to read,
and error messages don't announce themselves. We're legally required to be accessible, and
we're not. This is both the right thing to do and a real liability — every resident who
can't use the form is a person denied access to a public benefit.

## What I wish existed
I want **anyone** to be able to complete this application independently — including people
who use a screen reader, people who navigate only by keyboard, and people with low vision.
Someone using assistive technology should be able to start the form, understand every field,
recover from mistakes, and submit successfully, on their own, without calling us for help.
I need to be able to stand in front of legal and say, credibly, that we meet the recognized
accessibility standard.

## Who would use it
- **Residents with disabilities** — screen-reader users, keyboard-only users, people with
  low vision or color blindness — who must be able to apply unaided.
- **All other residents** — the form has to keep working normally for everyone else.
- **Me and our legal team** — who need evidence we can point to that it now conforms.

## Constraints & must-haves
- Must meet the recognized accessibility bar our regulators reference (the standard legal
  will name). "Looks better" isn't enough — it has to actually conform and be demonstrable.
- The form must keep collecting exactly the same information and submitting the same way.
  We can't change what we ask or drop a field; this is about access, not scope.
- It can't require residents to install anything or use a special "accessible version."
  One site that works for everyone — not a separate stripped-down page.
- We need a way to *check* accessibility going forward, so we don't quietly regress the next
  time someone edits the form.

## What success looks like
- A screen-reader user and a keyboard-only user can each complete and submit the real form
  end to end, unaided, in testing.
- We can produce a conformance check against the required standard with no blocking failures.
- The next change to the form gets flagged automatically if it breaks accessibility, before
  it ships.

## Out of scope (for now)
- Redesigning the look of the site or restructuring the application process. Keep what we
  ask and how it flows; make it usable by everyone.
- Translating the form into other languages. Important, but a separate effort from access.
