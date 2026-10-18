# HoldLine

**An AI voice assistant that reserves real bakery inventory over the phone.**

When you phone a bakery and ask whether they have chocolate cake, "yes" is a statement about the
past. By the time you arrive it may be false, and nobody is accountable for it. That is
*information*. What the caller needed was a *promise*: a specific item, reserved under their name,
that someone is accountable for. HoldLine reads live inventory, states exactly how many of an item
are sellable, and places a time-limited hold that decrements availability for every subsequent
caller. Staff see every conversation and approve anything outside policy.

## Headline results

Not measured yet. The evaluation harness (Phase 7) writes `evals/RESULTS.md` with the guardrail-off
and guardrail-on columns; this section is replaced by that table once the recording run has been
made. No number appears here that was not produced by code in this repository.

## Status

| Phase | Tag | State |
|---|---|---|
| 1 Foundation | `v0.1-foundation` | done |
| 2 Domain core | `v0.2-domain` | done, concurrency test 20/20 |
| 3 Agent | | in progress |

## The three hard problems

1. **Grounding.** The assistant must be structurally incapable of stating a number it did not
   retrieve from a tool. Not discouraged from it: incapable.
2. **Safety tiering.** A wrong cake count is embarrassing. A wrong allergen answer can kill
   someone. They get different failure policies: quantity claims retry, allergen claims hard-block
   to a human and never retry.
3. **Concurrency.** Three phone lines, a walk-in customer and a delivery driver can race for the
   last cake. Exactly one must win.

## Domain model (Phase 2)

```
sellable = on_hand - active holds - pending-approval holds - safety buffer
```

* `availability.py` enforces the **staleness gate**: POS data older than 10 minutes returns
  `status: unknown` and no number is ever produced. This is code, not a prompt instruction.
* `holds.py` makes `create_hold` **atomic**. The availability check runs *inside* a
  `BEGIN IMMEDIATE` transaction, so the check and the insert happen under one write lock. Checking
  first and writing second leaves a time-of-check-to-time-of-use window in which another caller
  takes the last unit. `tests/test_concurrency.py` fires ten real threads through a barrier at one
  sellable unit, twenty times, and asserts exactly one active hold each time.
* Holds are **idempotent** on `(call_id, location, item, quantity, pickup minute)`. The model
  sometimes calls `create_hold` twice in one turn; the ledger returns the existing hold instead of
  promising the same cake twice.
* `dietary.py` **never judges safety**. It returns what an item declares. The catalogue includes a
  brownie flagged gluten-free that declares wheat as a cross-contact risk, because a menu flag is not
  a safety claim.

## Setup

```
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # add ANTHROPIC_API_KEY only for live conversations
python -m holdline.seed         # creates holdline.db with one location and twelve items
pytest                          # no network, no API key needed
ruff check . && ruff format --check .
```

## Honest limitations

Mocked POS. Browser voice is Chrome and Edge only. One location served by the app. No
authentication on the staff page. SMS is a documented stub.
