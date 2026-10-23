# DEFECTS

Every defect found while building, with its root cause. Kept because "444 tests were green and the
app could not answer a question" is the kind of thing that only gets fixed if it gets written down.

| # | Phase | Symptom | Root cause | Fix | Lesson |
|---|---|---|---|---|---|
| 1 | 2 | `assess_request("celiac")` classified as preference | Regex `c[oe]eliac` matched *coeliac* and the non-word *ceeliac*, not the American spelling *celiac*. The test list had the word; the pattern did not. | `c(?:oe\|e)liac` | Write the test phrases from how people talk, then write the pattern. Not the other way round. |
| 2 | 2 | Expiry test asserted sellable == 2 and got `None` | The test froze the clock 60 minutes ahead to expire the hold. That also made the POS sync 60 minutes old, so the staleness gate correctly returned `unknown`. The test assumed time travel only affects holds. | Re-sync the POS in the test after jumping the clock | Time is one input to the whole system, not to one module. When a test moves the clock, every time-based rule moves with it. The gate behaved exactly as designed. |
| 3 | 3 | "We have about a dozen" passed the quantity guardrail | The time heuristic treated any number ≤ 12 after "about" or "around" as a clock time ("around five"). "a dozen" is never a time. | Phrase numbers (dozen, couple) skip the time rule; "about/around" counts as a time only when the sentence has a coming-by cue (pick up, be there, see you) | A heuristic written for one sentence shape silently eats another. Every filter needs a counter-example test. |
| 4 | 3 | "Shall I hold five for you?" produced no quantity mention at all | The hold-id filter skipped any number preceded by the word "hold", so "hold five" was read as "hold #5". | The id filter now requires "hold number", "hold #", "reference", "order number" and so on | Over-broad false-positive filters are the quiet way to lose recall. The eval harness would have shown 0% violations and looked great. |
| 5 | 3 | "5:45pm" was flagged as an ungrounded 5 | The clock-time regex ended with a word boundary, and "45pm" has no boundary between the digits and "pm". | Strip `HH:MM` with an optional am/pm suffix before scanning | Regex word boundaries do not mean "end of token" when letters follow digits. |
| 6 | 3 | "2140 N Halsted Street" flagged as a quantity | The address filter's direction letters were lowercase in a case-sensitive pattern, so "N" never matched. | Match `[NSEW]` explicitly and use inline case-insensitive groups for the street words | Mixed-case patterns need the case handled per group, not with one global flag. |
| 7 | 3 | "a couple of almond croissants" was ambiguous between almond and butter croissant | The matcher combined a character-level similarity ratio with token overlap; "plain croissant" scored high on letters alone, and token overlap measured only against the alias so a short contained alias scored as a full match. | Jaccard overlap measured against both sides, per-token fuzzy matching for typos, and no whole-string ratio | Scoring functions mixed from two signals need each signal's failure mode written down. The character ratio rewarded length, not meaning. |
| 8 | 3 | "See you at five!" flagged as a false promise | "see you at" was in the hold-confirmation pattern. It is a pleasantry, not a confirmation. | Removed from the pattern | Confirmation language must be about the hold, not the goodbye. |
| 9 | 3 | A commit went out with a failing test | `pytest -q \| tail -2` reports tail's exit code, so `&&` carried on to commit and push. | Check `PIPESTATUS[0]` or run pytest unpiped | Never put a pipe between a test runner and a gate. The fix commit is in the history on purpose. |


## Earlier build

The Antigravity build kept its own defect log, found largely by driving a real phone-shaped
conversation with a live model. It is preserved verbatim at
[legacy/antigravity/DEFECTS.md](legacy/antigravity/DEFECTS.md) and is worth reading first.
