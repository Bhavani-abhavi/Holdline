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
    r"\bcan'?t (?:have|eat|do)\b",
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


def assess_request(dietary_text: str | None) -> DietaryAssessment:
    """Classify a caller's stated dietary need.

    Allergy patterns win over preference patterns: "I'm vegan and allergic to
    nuts" is an allergy. Empty or whitespace text is "none".
    """
    text = (dietary_text or "").strip().lower()
    if not text:
        return DietaryAssessment(severity="none", requires_human=False)
    for pattern in ALLERGY_PATTERNS:
        if re.search(pattern, text):
            return DietaryAssessment(severity="allergy", requires_human=True, matched=pattern)
    for pattern in PREFERENCE_PATTERNS:
        if re.search(pattern, text):
            return DietaryAssessment(severity="preference", requires_human=False, matched=pattern)
    # Unrecognised text is still a dietary note the caller chose to give. Treat it
    # as a preference so it reaches staff verbatim; it does not block the hold.
    return DietaryAssessment(severity="preference", requires_human=False, matched=None)
