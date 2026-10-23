# Defects the tests caught

A running record of bugs found by writing tests, with what each one would have done in
production had it shipped.

It is here for two reasons. It is the honest version of "the tests pay for themselves"
— each entry is a specific failure that a specific test found before a caller did. And
it keeps the reasoning attached to the fix: every entry below corresponds to a test
named for the behaviour it protects, so removing the test and removing the protection
are the same edit.

None of these were found by the type checker, the linter, or by reading the code.
Five were found by asserting on behaviour, one by driving the scripted system by hand,
and **four by making a single real phone-shaped conversation with a live model** -- which
is the strongest argument in this file for doing that before calling a phase done.

---

## 1. Masking a price merged two sentences

**Phase 3 · `guardrails.py` · [test_masking_a_price_does_not_merge_two_sentences](tests/test_guardrails.py)**

The guardrail blanks out everything that is provably not a quantity — times, prices,
phone numbers — and then checks what survives. The first implementation masked the
whole reply and *then* split it into sentences.

A price contains a full stop. `"It costs $24.00. Want me to hold two?"` masks to
`"It costs        Want me to hold two?"`, and the sentence boundary is gone with it.
The masked text had one sentence where the original had two, so pairing them by index
put every later sentence against the wrong original.

**What it would have done in production.** The assertion-versus-proposal decision is
made per sentence. After any sentence containing a price, that decision would have been
read off the wrong sentence for the rest of the reply. Concretely: `"It costs $24.00.
We have thirty left."` — the second sentence would inherit the first's classification.
Depending on which way the misalignment fell, either an ungrounded stock claim passes
because it was mistaken for an offer, or a legitimate offer is blocked as an
unsupported assertion. Both are wrong, neither is deterministic from reading the code,
and the symptom would have been *intermittent guardrail behaviour that correlated with
whether the assistant happened to mention a price* — close to undiagnosable from a bug
report.

**Fix.** Split the original text into sentences first, then mask each sentence
independently. Masking still preserves offsets within a sentence, so reported spans
still point at real text.

---

## 2. Every "I'm on my way" hold was refused

**Phase 3 · `holds.py` · [test_a_hold_beyond_what_is_sellable_is_refused_with_a_reason](tests/test_tools.py)**

`spoken.parse_pickup("now")` resolves an as-soon-as-possible pickup to the instant it
ran. `holds.create_hold` then reads the clock again and rejects any pickup time earlier
than "now", on the reasoning that a past pickup means the spoken time was resolved
against the wrong day.

Those two clock reads are microseconds apart, and the second one is always later. So
the check fired on its own latency.

**What it would have done in production.** Every hold where the caller said "now",
"right away", "on my way", "in a bit", or gave no pickup time at all would come back
`ok=False, reason="pickup_in_past"`. That is the single commonest pickup case in a
bakery. The assistant would have told those callers it could not place the hold, with
no explanation any human could act on, while holds for "five o'clock" worked perfectly
— so the failure would look like a mysterious partial outage rather than a clock bug.

Worth noting how it surfaced: the test that caught it was not testing pickup times at
all. It was asserting that an over-large order is refused with `reason="insufficient"`,
and it failed because it got `pickup_in_past` instead. The bug was found by a test
looking somewhere else entirely.

**Fix.** `config.PICKUP_PAST_TOLERANCE_SECONDS` (120s). A pickup time inside the
tolerance is clamped to now rather than refused; one outside it is still rejected,
because a genuinely misread time is hours out, not seconds. The threshold carries its
reasoning in `config.py`.

---

## 3. "No allergies" escalated as an allergy

**Phase 3 · `dietary.py` · [test_a_caller_saying_they_have_no_allergies_is_not_escalated](tests/test_tools.py)**

The allergy classifier is deliberately over-inclusive: any allergen word, any illness
word, any "can't have" escalates. The string `"allerg"` is one of those markers — and
`"no allergies"` contains it.

The first implementation handled this by requiring a denial to match the **whole**
answer, which is correct for `"no allergies"` on its own and correct for
`"no nut allergies but she can't have dairy"` (not a whole-answer denial, so it
escalates, rightly). It broke the moment the denial was one clause of several:

```
"Yes, around five. No allergies."   →  allergy      (should be none)
"Do you have chocolate cake?"       →  preference   (should be none)
```

**What it would have done in production.** The first case escalates every caller who
was asked about allergies and said they had none — which is most callers. Each one gets
routed to a human, gets an urgent `allergy_callback` alert raised, and has their hold
held as `pending_approval` instead of confirmed. The feature would have been worse than
useless: staff would be paged for every ordinary order, and would learn within a day to
ignore the urgent alerts — including the real ones. **An over-inclusive safety
classifier that fires constantly is not a cautious system; it is a system whose safety
alerts nobody reads.**

The second case is quieter and nearly as bad. `note_customer_turn` runs the classifier
on *every* caller utterance, and unparseable text was being classified as a
"preference". So any ordinary question set a dietary context on the conversation, which
armed the context-gated tier-2 reassurance rule. `"That's fine, five o'clock works"`
would then have been a hard safety block with no retry, on a call where nobody had
mentioned food allergies at all.

**Fix.** Two changes. Classification is now clause by clause: a denial cancels only the
clause it appears in, so a denial among clauses that say nothing dietary means "none",
while a declaration in another clause still escalates. And `assess_request` takes
`direct_answer`, distinguishing "this text is the caller's answer to the allergy
question" (unparseable content becomes a preference, so the kitchen still hears it)
from "this is an arbitrary turn of conversation" (unrecognised means nothing dietary was
raised).

---

## 4. "Your hold number is 7" read as seven cakes

**Phase 3 · `guardrails.py` · [test_non_quantity_numbers_are_not_treated_as_quantity_claims](tests/test_guardrails.py)**

The mask for hold references required the number to follow the keyword directly —
`hold 7`, `hold number 7`, `order #7`. It did not allow a copula, so
`"Your hold number is 7."` left the `7` exposed as a bare numeral.

**What it would have done in production.** A hold id is not in the tool results as a
quantity, so the `7` would read as an ungrounded stock claim. The assistant would be
blocked from telling a caller their own reference number — the last sentence of a
successful booking — and after the retry it would fall back to "let me check and call
you back" on a call that had just completed perfectly. The caller would be left without
the number they need to collect their cake.

**Fix.** An optional `(?:is|was|:)` in the pattern. Staff and callers phrase it both
ways.

---

## 5. The glass box went blank exactly when a violation occurred

**Phase 3 · `agent.py` · [test_a_blocked_turn_reports_its_violations_to_the_glass_box](tests/test_agent.py)**

`ValidationResult` defines `__bool__` returning `self.ok`, so that calling code can
write `if validate_response(...)`. `Turn.as_dict()` -- which builds the payload the
customer page's live panel renders -- then wrote:

```python
"violations": self.validation.as_dict()["violations"] if self.validation else [],
"guardrail_tier": self.validation.tier if self.validation else 0,
```

For a *failed* validation, `if self.validation` is `False`. So the branch meant to
handle "there is no validation" ran whenever there was a validation that had failed.

**What it would have done in production.** The live panel is described in the brief as
"the single most important demo asset in the project" -- the thing an interviewer watches
to see `check_availability` return `11` a half-second before the voice says "eleven",
with guardrail violations shown in red with their tier. This bug meant it reported
`violations: []` and `tier: 0` for every blocked turn. The panel would have looked
perfect during normal operation and shown nothing at all during a guardrail block: the
reply would silently change to the fallback text with no indication why. The one moment
the feature exists to demonstrate is the one moment it displayed nothing.

Worth noting it was found by a *walkthrough script*, not by the test suite. The suite
asserted on `turn.validation.tier` directly and was correct; nothing asserted on
`as_dict()`, which is the shape the UI actually consumes. It surfaced because the
walkthrough printed `tier 0` next to a line that had obviously been blocked, and the
number was wrong in a way a human reading output notices immediately and an assertion
on a different field never would.

**Fix.** Explicit `is not None` checks, with a comment saying why the obvious form is
wrong. Plus four tests on the `as_dict()` payload itself, since the gap was that
nothing tested the boundary the UI reads.

**The general lesson.** Defining `__bool__` on a result object makes `if result:` mean
"is valid", which is useful, and silently breaks every `if result:` that meant "exists".
Either is defensible; having both in one codebase is not.

---

## 6. Saying "no allergies" sent the order to a manager

**Phase 3 · `holds.py` · [test_a_caller_saying_they_have_no_allergies_is_not_escalated](tests/test_tools.py)**

`needs_approval` implemented the specification literally: a hold needs a human when
"a dietary note is present". It tested for the presence of *text*:

```python
if dietary_note and dietary_note.strip():
    reasons.append("dietary_note_present")
```

The agent is instructed to always ask about allergies before finalising a hold and to
pass whatever the caller says verbatim. So the commonest possible value of that field is
`"no allergies"` -- and that is text, so it forced approval.

**What it would have done in production.** Every caller who answered the allergy
question and said they had none would have their hold parked in `pending_approval`
instead of confirmed, with an `approval_needed` alert raised for staff. Meanwhile the
assistant, having been told the hold succeeded, would say "you're all set" -- about a
hold nobody had approved. So: the approval queue fills with ordinary orders, staff stop
reading it, and the callers most likely to be told "all set" are the ones whose hold is
least confirmed.

**Fix.** `needs_approval` now keys off the assessed severity rather than the presence of
characters. `"none"` -- which includes an explicit denial -- does not force approval;
`"preference"` and `"allergy"` still do. This is a deliberate departure from the
specification's wording, recorded in the function's docstring.

**Why it was not caught earlier.** Defect 3 fixed the *classifier* so that "no
allergies" assesses as `none`. This is the same mistake one layer up: the ledger was not
asking the classifier at all, it was asking whether the string was empty. Fixing a
classifier does nothing for a caller who never consults it.

---

## 7. One schema keyword broke every conversation

**Phase 3 · `tools.py` · [test_no_tool_schema_uses_a_keyword_strict_mode_rejects](tests/test_tools.py)**

`create_hold`'s quantity property carried `"minimum": 1`. Sensible JSON Schema, and
`strict: true` rejects it:

```
400 invalid_request_error: tools.3.custom: For 'integer' type, property 'minimum' is not supported
```

**What it would have done in production.** Nothing worked. Not one turn of one
conversation. The tool list is sent with every request, so a single unsupported keyword
anywhere 400s everything identically -- and the symptom is a total outage that looks
like a provider problem rather than a schema mistake.

**The part worth keeping.** This is the only class of bug in the project that offline
tests structurally cannot see, because the schema is validated by the API and by nothing
else. 444 tests were green while the application could not answer a single question.

It is also the entry that best demonstrates the degradation design working: six
conversations, every turn a 400, zero crashes, and every caller got
*"I want to get this exactly right rather than guess, so let me check with someone here
and call you right back."* The failure was total and the behaviour was still correct.

**Fix.** The keyword is gone, the floor is stated in the description and enforced in
`holds.create_hold` -- which is the right place anyway, since a schema can express "at
least 1" but not "at least 1 and no more than is sellable right now". Plus a test that
walks every generated schema for the full set of keywords strict mode rejects, so the
next one is caught offline.

---

## 8. "And no allergies" escalated as an allergy

**Phase 3 · `dietary.py` · [test_a_denial_introduced_by_a_conjunction_is_still_a_denial](tests/test_dietary_safety.py)**

Defect 3 made denials work clause by clause. Every test for it used the bare phrasing --
`"no allergies"`, `"none"`, `"not really"`. A live caller says:

> "Yes please, hold one for me. I'm Dana, I'll be there in about twenty minutes, **and**
> no allergies."

The clause is `"and no allergies"`. A denial has to match its whole clause, and with the
conjunction attached it does not, so it fell through to the medical markers again.

**What it would have done in production.** Defect 3's symptom, restored in full: the
caller who explicitly said they had none gets routed to a human with an urgent alert.
Worse than defect 3 in one respect -- the fix was already in place and looked correct,
and only a real sentence exposed that it had been tested exclusively against phrasings
nobody uses out loud.

**Fix.** Leading discourse markers are stripped from each clause before the denial check:
and, so, plus, oh, well, yeah, just, um. Conjunctions only -- a leading "no" or "not" is
meaning and is never stripped, which is the direction that would have been dangerous.

---

## 9. "Twenty minutes" contained a nut allergy

**Phase 3 · `dietary.py` · [test_an_allergen_name_inside_an_ordinary_word_is_not_a_declaration](tests/test_dietary_safety.py)**

Allergen names were matched as substrings. `"nut"` is inside `"mi-nut-es"`.

```
"I'll be there in twenty minutes"  ->  allergy   matched=['nut']
"give me a minute"                 ->  allergy   matched=['nut']
"we're just round the corner"      ->  allergy   matched=['corn']
```

**What it would have done in production.** This is the worst of the ten. "Twenty minutes"
is in a large share of pickup conversations -- it is the standard answer to "when will you
come by?". Every one of those callers would have been treated as declaring a nut allergy:
hold parked for approval, manager paged, urgent callback raised, and the assistant
refusing to confirm the booking it had just made. The alert queue would have been almost
entirely noise within an hour of going live.

It also explains the live transcript more honestly than defect 8 does. Both bugs were
present in the same sentence, and this one was doing most of the damage.

**Fix.** Allergen names are matched as words with an optional plural, not as substrings.
The medical markers stay substrings deliberately -- they are distinctive stems whose
purpose is catching inflections ("allerg" -> allergy, allergic, allergen) and none of them
collides with an ordinary word.

---

## 10. The guardrail punished the assistant for being careful

**Phase 3 · `guardrails.py` · [test_a_third_party_buying_one_is_a_hedge_not_a_stock_claim](tests/test_guardrails.py), [test_offering_to_hold_one_works_when_the_item_is_still_ambiguous](tests/test_guardrails.py)**

Two false positives, both found by a live model behaving well.

Asked *"are you absolutely certain?"*, the model re-checked and answered:

> "I just re-checked and it's still showing 11. That's what our system has this minute --
> things can sell on the floor, so if **someone buys one** at the counter…"

That "one" is a pronoun. It was read as an ungrounded quantity claim, and the honest
caveat -- exactly what a good cashier adds -- was replaced with the safe fallback.

Asked to hold a croissant, `check_availability` correctly returned *ambiguous* with two
candidates and **no quantity**. With no quantity there is no ceiling for a proposal, so
*"which one would you like me to hold?"* was blocked -- the precise question the ambiguous
result exists to prompt. The ambiguity feature defeated itself.

**What it would have done in production.** Neither is a safety failure; both are worse
than they look. The assistant would have been least able to speak exactly when it was
being most careful -- hedging an answer, or asking which item was meant. A guardrail that
punishes good behaviour trains its owner to loosen it.

**Fix.** Two narrow changes. A third-party subject makes "one" pronominal ("someone buys
one"), and that alternative has to come *first* in the pattern, because alternation is
left-to-right and the bare "someone" branch would otherwise consume just the subject and
leave "buys one" exposed -- which is how the first attempt at this fix silently did
nothing. And a proposal of exactly one unit is allowed without a ceiling, because
offering a single unit asserts nothing about how many exist and `create_hold` settles it.
Rule 3 still bans every specific quantity under stale data, proposals included, so the
exemption opens no hole where the real risk is. Four tests hold both boundaries from
each side.

---

## 11. A duplicate create_hold reserved the same cake twice

**Phase 3 · `holds.py` · [test_the_same_request_twice_in_one_call_produces_one_hold](tests/test_holds.py)**

Not a code defect -- an absent guarantee. The live transcript showed the model calling
`create_hold` twice for one request and then cancelling the second itself:

```
[tool] create_hold -> {'ok': True, 'hold_id': 1, 'status': 'active'}
[tool] create_hold -> {'ok': True, 'hold_id': 2, 'status': 'active'}
[tool] cancel_hold -> {'ok': True, 'hold_id': 2, 'status': 'cancelled'}
```

The net result was correct, which is exactly what made it easy to wave through.

**What it would have done in production.** Between the second create and the cancel,
two units were committed against a caller who had been promised one. A concurrent caller
reading availability in that window is refused stock that is not really spoken for. And
the correction depended entirely on the model choosing to notice and clean up: on a turn
where it did not, the duplicate would sit in the ledger until expiry, quietly holding a
cake for nobody.

**Fix.** `create_hold` derives an idempotency key from
`(call_id, location_id, item_id, quantity, pickup minute)`. A second call with the same
key returns the existing hold with `deduplicated=True` instead of writing a new row. The
lookup happens inside the same `BEGIN IMMEDIATE` transaction as the availability check,
and a **partial** unique index on the key backs it at the database level -- partial so the
constraint applies only while a hold is live, because a caller who collects and rings
back for another is not a duplicate.

The pickup time is truncated to the minute. Two resolutions of "in twenty minutes" in one
turn land microseconds apart, so a key built from the raw instant would differ and dedupe
nothing.

**What it cost.** A caller who genuinely wants a second separate hold of the same item at
the same minute of the same call gets the first one back. They would have said "two", so
the trade is worth making, and it is documented at the key derivation rather than left to
be discovered.

---

## What the pattern says

Three of the four are **interaction** bugs rather than logic bugs. Each component was
correct alone: the masker masked correctly, the clock comparison was sound, the
classifier's rules were right for the input they were designed for. They failed at the
seams — masking composed with sentence-splitting, the parser's clock composed with the
ledger's clock, a classifier written for one input reused for another.

That is the argument for the tests being written against *behaviour* rather than
against functions. None of these four would have been caught by a unit test of the
component that contained the bug, because no component contained it.

The fourth is different in kind and worth keeping for contrast: an ordinary incomplete
pattern, found by enumerating the false-positive cases one at a time rather than
trusting that the list was complete.
