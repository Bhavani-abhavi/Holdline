# INTERVIEW.md

## The 90-second pitch

When you phone a bakery and ask whether they have chocolate cake, "yes" is a statement about the
past. By the time you arrive it may be false, and nobody is accountable. HoldLine turns that
information into a promise. It answers the phone, reads live stock, says exactly how many are
sellable, and places a time-limited hold that every later caller sees.

Three things are hard. Grounding: the assistant has to be structurally unable to state a number it
did not get from a tool, so a validator runs after generation and checks every numeral and number
word against this turn's tool results. Safety: a wrong cake count and a wrong allergen answer are
different mistakes, so the validator has two tiers. Quantity slips retry once, then fall back.
Allergen claims hard-block to a human and never retry, because a regenerated answer that happens to
pass is still a model guessing about anaphylaxis. Concurrency: three callers can race for the last
cake, so the availability check runs inside the write transaction under `BEGIN IMMEDIATE`, and a
ten-thread barrier test proves exactly one wins, twenty times.

I measured it. The eval harness records the model once and replays the same responses with the
guardrail off and on, so the only variable in the two columns is the guardrail.

## Twelve likely questions

**1. Why tool grounding instead of a better prompt?**
A prompt is a request. The validator in `guardrails.py` is a guarantee that runs on every reply.
The system prompt documents intent; the code enforces it. Warmth and grounding are in tension, and
a longer prompt makes the model chattier, not more honest.

**2. How does the guardrail work?**
It extracts every numeral and number word, filters false positives (times, prices, phone numbers,
dates, hold ids, addresses, durations, sizes, idioms like "one moment"), classifies each as an
assertion or a proposal, and checks assertions against the numbers tools returned this turn plus
numbers the caller said. Proposals are bounded by the last known availability. Separately it looks
for hold-confirmation language without a successful `create_hold`, and for allergen absence claims.

**3. Why two tiers?**
Consequence. A wrong count costs an apology. A wrong allergen answer can kill. Tier 1 retries once
then falls back; Tier 2 never retries and escalates to a person. There is no code path from Tier 2
to a retry, by design.

**4. Explain the race condition.**
Check-then-write without a lock is a time-of-check-to-time-of-use race: two callers both read
"1 left" and both insert. `BEGIN IMMEDIATE` takes SQLite's write lock before the read, so the check
and the insert are one step. `tests/test_concurrency.py` fires ten threads through a barrier at one
sellable unit, twenty times, and asserts one active hold.

**5. Why does the assistant refuse to answer allergy questions?**
It relays what an item declares and never judges safety. The catalogue has a brownie flagged
gluten-free that declares wheat cross-contact; a menu flag is not a safety claim. A false positive
on escalation costs one callback; a false negative could cost a life.

**6. What is "assertion versus proposal"?**
"We have eleven" is settled by a tool result and must match it. "Want me to set one aside?" is
settled by the action that follows, and `create_hold` refuses what it cannot honour. Without the
split the assistant could never offer anything.

**7. Why idempotent holds, and why truncate pickup to the minute?**
The model sometimes calls `create_hold` twice in a turn. The key is looked up inside the same
transaction and backed by a partial unique index. Two resolutions of "in twenty minutes" differ by
microseconds, so the raw instant would never match.

**8. How is the eval honest about cost and sampling?**
Record once, replay forever. Both columns come from the same recorded responses; replay makes zero
API calls. The recording pass reports measured tokens split into cached reads, uncached input and
output. The guardrail-on column is bounded by validator recall, so ledger-based metrics are reported
separately.

**9. What would you change for production?**
Postgres, a real POS adapter, authentication on the staff page, SMS, a checkpointer so a dropped
call resumes, and tracing joined to the event log so I can ask whether violations cluster by item or
time of day.

**10. How would you scale it?**
The ledger is the bottleneck by design: one writer per location. Shard by location. The agent is
stateless per turn apart from the conversation history, so it scales horizontally.

**11. What would you monitor?**
Ungrounded quantity rate per week, Tier 2 block rate, hold collection rate, POS sync age, and the
double-call rate as a model-behaviour statistic. The events table is append-only with a monotonic
id so a pipeline can extract incrementally.

**12. What broke during the build?**
`DEFECTS.md`. The highlights: "celiac" was not matched because the regex was written for the
British spelling; "about a dozen" was being treated as a clock time; "hold five for you" was being
read as a hold id; and a test that jumped the clock to expire a hold also made the POS data stale,
which the gate correctly reported.

## Files worth pointing at

* `src/holdline/guardrails.py`: the two tiers and the false-positive filters.
* `src/holdline/holds.py`: `create_hold`, the transaction, the idempotency key.
* `src/holdline/dietary.py`: presence-only policy.
* `src/holdline/agent.py`: the loop; Tier 2 breaks out before any retry.
* `tests/test_concurrency.py`, `tests/test_guardrails.py`.
* `evals/run_evals.py`: record and replay, both columns from one recording.
* `DEFECTS.md`, `docs/adr/`.
