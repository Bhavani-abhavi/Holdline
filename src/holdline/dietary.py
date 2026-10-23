"""Dietary and allergen policy. Small module, most consequential code in the project.

Policy, enforced in code:

* `check_dietary_info` returns what an item DECLARES: contains, may_contain,
  flags. It makes no inference and never computes "is this safe for X". That
  judgment belongs to a human.

* `assess_request` classifies what a caller said as none / preference / allergy.
  It is deliberately over-inclusive: anything resembling an allergy,
  intolerance, celiac disease or anaphylaxis is "allergy". A false positive
  costs one phone call. A false negative could cost a life. The asymmetry is the
  entire design rationale.

* When an allergy is declared the hold is created as pending_approval with an
  allergy_callback alert (holds.py), and the guardrail blocks any statement that
  an item is free of, safe for or suitable for the allergen (guardrails.py).

Why presence, never absence: the seeded catalogue has a brownie flagged
"gluten-free" that declares wheat as a cross-contact risk. The flag means what
the bakery wrote on the menu, not what a caller with celiac disease would take
it to mean. So the assistant may say what an item contains, and may never say
what it lacks.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Literal

from holdline.exceptions import UnknownItemError

Severity = Literal["none", "preference", "allergy"]

# Over-inclusive on purpose. "can't have" and "reaction" are vague, and that is fine:
# the cost of over-escalating is one callback.
ALLERGY_PATTERNS = [
    r"allerg",
    r"anaphyla",
    r"c(?:oe|e)liac",
    r"intoleran",
    r"epi.?pen",
    r"\bcan'?t (?:have|eat)\b",
    r"\bcannot (?:have|eat)\b",
    r"\bnot allowed\b",
    r"\breaction\b",
    r"\bsensitiv",
    r"\bbreak out\b",
    r"\bswell",
    r"\bgluten[- ]free\b.*\b(?:need|must|has to|have to)\b",
    r"\b(?:need|must|has to|have to)\b.*\bgluten[- ]free\b",
    r"\bwill (?:this|that|it) (?:work|be ok|be okay|be fine|be safe)\b",
    r"\bis (?:this|that|it) (?:safe|ok|okay)\b",
]

PREFERENCE_PATTERNS = [
    r"\bvegan\b",
    r"\bvegetarian\b",
    r"\bhalal\b",
    r"\bkosher\b",
    r"\bdairy[- ]free\b",
    r"\bsugar[- ]free\b",
    r"\blow[- ]sugar\b",
    r"\bprefer",
    r"\btrying to avoid\b",
    r"\bavoiding\b",
    r"\bdiet\b",
    r"\bno (?:nuts|dairy|eggs|gluten|wheat|soy|sesame|peanuts)\b",
]


@dataclass(frozen=True)
class DietaryInfo:
    """What an item declares. Straight from the database, no inference."""

    item_id: str
    item_name: str
    contains: list[str] = field(default_factory=list)
    may_contain: list[str] = field(default_factory=list)
    dietary_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "item_name": self.item_name,
            "contains": list(self.contains),
            "may_contain": list(self.may_contain),
            "dietary_flags": list(self.dietary_flags),
            # The note travels with every result so the model sees the policy at the
            # moment it matters, not only in the system prompt.
            "policy": (
                "Relay declared ingredients only. Never state that an item is free of, "
                "safe for, or suitable for any allergy. A person confirms allergies."
            ),
        }


@dataclass(frozen=True)
class DietaryAssessment:
    """Classification of a caller's stated need."""

    severity: Severity
    requires_human: bool
    matched: str | None = None

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "requires_human": self.requires_human,
            "matched": self.matched,
        }


def check_dietary_info(conn: sqlite3.Connection, item_id: str) -> DietaryInfo:
    """Return declared contains / may_contain / flags for an item. No inference."""
    row = conn.execute(
        "SELECT id, name, contains, may_contain, dietary_flags FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if row is None:
        raise UnknownItemError(item_id)
    return DietaryInfo(
        item_id=row["id"],
        item_name=row["name"],
        contains=json.loads(row["contains"]),
        may_contain=json.loads(row["may_contain"]),
        dietary_flags=json.loads(row["dietary_flags"]),
    )


# A denial cancels only the clause it appears in. Matched against the whole clause
# after leading discourse markers are stripped, so "and no allergies" is a denial
# while "no nut allergies but she can't have dairy" is not.
DENIAL_RE = re.compile(
    r"^(?:no|nope|nah|none|nothing|not really|no allerg(?:y|ies)|no dietary (?:needs?|restrictions?|requirements?)|"
    r"no (?:food )?(?:allergies|restrictions|issues|problems)|none (?:that i know of|at all|whatsoever)|"
    r"(?:we'?re|i'?m|she'?s|he'?s|they'?re) (?:all )?(?:good|fine)|all good|nothing like that|"
    r"no(?:,)? (?:nothing|none|we'?re good|i'?m good|all good))[.!]?$",
    re.IGNORECASE,
)
DISCOURSE_MARKERS_RE = re.compile(
    r"^(?:(?:and|so|plus|oh|well|yeah|yes|yep|just|um|uh|also|but|then|okay|ok)[,\s]+)+",
    re.IGNORECASE,
)
CLAUSE_SPLIT_RE = re.compile(r"[,.;!?]+|\s+(?:and|but|plus)\s+", re.IGNORECASE)


def _clauses(text: str) -> list[str]:
    """Split on punctuation and conjunctions, strip leading discourse markers."""
    out = []
    for raw in CLAUSE_SPLIT_RE.split(text):
        clause = DISCOURSE_MARKERS_RE.sub("", raw.strip()).strip()
        if clause:
            out.append(clause)
    return out


def assess_request(dietary_text: str | None, *, direct_answer: bool = True) -> DietaryAssessment:
    """Classify a caller's stated dietary need.

    Works clause by clause: a denial ("no allergies") cancels only its own clause,
    so "yes, around five, and no allergies" is "none" while "no nut allergies but
    she can't have dairy" still escalates. Allergy wins over preference: "I'm vegan
    and allergic to nuts" is an allergy.

    Args:
        direct_answer: True when the text answers the allergy question (a
            `dietary_note`); unrecognised text is then a preference so the kitchen
            still hears it. False for an arbitrary turn of conversation, where
            unrecognised text means nothing dietary was raised.
    """
    text = (dietary_text or "").strip().lower()
    if not text:
        return DietaryAssessment(severity="none", requires_human=False)
    saw_preference: str | None = None
    saw_denial = False
    for clause in _clauses(text):
        if DENIAL_RE.match(clause):
            saw_denial = True
            continue
        for pattern in ALLERGY_PATTERNS:
            if re.search(pattern, clause):
                return DietaryAssessment(severity="allergy", requires_human=True, matched=pattern)
        if saw_preference is None:
            for pattern in PREFERENCE_PATTERNS:
                if re.search(pattern, clause):
                    saw_preference = pattern
    if saw_preference:
        return DietaryAssessment(
            severity="preference", requires_human=False, matched=saw_preference
        )
    if saw_denial or not direct_answer:
        return DietaryAssessment(severity="none", requires_human=False)
    # A direct answer we could not parse is still something the caller chose to say.
    return DietaryAssessment(severity="preference", requires_human=False, matched=None)
