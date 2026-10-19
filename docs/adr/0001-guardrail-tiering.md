# ADR 0001: Guardrails are code, tiered by consequence

**Status:** accepted. **Date:** 2026-10-18.

## Context

The assistant must not state a stock number it did not retrieve, must not confirm a hold the ledger
refused, and must never tell a caller that an item is safe for an allergy. A system prompt can ask
for all three. It cannot guarantee any of them: warmth and grounding are in tension, and a chattier
model improvises more.

A wrong cake count and a wrong allergen answer are also not the same kind of mistake. One costs a
sale or an apology. The other can put someone in hospital.

## Decision

`guardrails.validate_response` runs after generation and before anything reaches the caller.

* **Tier 1, grounding** (quantities, hold confirmation, stale override, ungrounded ingredient
  presence): retry once with an operator correction, then substitute the safe fallback script.
* **Tier 2, safety** (any claim that an item is free of, safe for, or suitable for an allergy;
  confirming a hold after an allergy was declared without a human in the loop): substitute the
  escalation script immediately, raise an urgent staff alert, never retry.

Two refinements were contested and decided:

* **Assertions versus proposals.** "We have eleven" must match a tool result exactly. "Want me to
  set one aside?" is a proposal whose truth is settled by `create_hold`, which refuses what it cannot
  honour. Proposals are bounded by the last known availability and forbidden under stale data.
* **Presence, never absence.** The assistant may say what an item contains and may never say what it
  lacks, even when `check_dietary_info` ran and even when the menu flags it. The seeded brownie is
  flagged gluten-free and declares wheat cross-contact; a flag is not a safety claim. Known
  consequence, accepted: the assistant cannot answer "is it vegetarian?" with a yes. It lists
  ingredients instead.

## Consequences

Tier 2 has no path to a retry. A regenerated answer that happens to pass validation is still a model
guessing about anaphylaxis; randomness is not a safety mechanism. The guardrail-on column of the
evaluation is bounded by the validator's recall, which is why the ledger-based metrics (escalation,
hold state) are reported separately and do not depend on the validator.
