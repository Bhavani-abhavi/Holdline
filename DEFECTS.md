# DEFECTS

Every defect found while building, with its root cause. Kept because "444 tests were green and the
app could not answer a question" is the kind of thing that only gets fixed if it gets written down.

| # | Phase | Symptom | Root cause | Fix | Lesson |
|---|---|---|---|---|---|
| 1 | 2 | `assess_request("celiac")` classified as preference | Regex `c[oe]eliac` matched *coeliac* and the non-word *ceeliac*, not the American spelling *celiac*. The test list had the word; the pattern did not. | `c(?:oe\|e)liac` | Write the test phrases from how people talk, then write the pattern. Not the other way round. |
| 2 | 2 | Expiry test asserted sellable == 2 and got `None` | The test froze the clock 60 minutes ahead to expire the hold. That also made the POS sync 60 minutes old, so the staleness gate correctly returned `unknown`. The test assumed time travel only affects holds. | Re-sync the POS in the test after jumping the clock | Time is one input to the whole system, not to one module. When a test moves the clock, every time-based rule moves with it. The gate behaved exactly as designed. |
