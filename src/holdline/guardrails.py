"""Tiered response validation. The centrepiece of the project.

Runs after generation, before the response reaches the caller. Two tiers with
different failure policies, because a wrong cake count and a wrong allergen
answer are not the same kind of mistake.

TIER 1, GROUNDING (recoverable: retry once, then safe fallback)
  1. QUANTITY GROUNDING. Every numeral and number-word the assistant utters as a
     quantity must appear in a tool result from THIS turn, or be a number the
     caller themselves said this turn.
  2. HOLD CONFIRMATION. Text confirming a reservation requires a successful
     create_hold result this turn, and a pending hold may not be presented as
     confirmed.
  3. STALE OVERRIDE. If availability came back "unknown", the reply may contain
     no specific quantity at all, not even as an offer.
  Plus ungrounded PRESENCE claims ("it contains walnuts" with no dietary lookup):
  over-warning costs a sale, not a life, so this is Tier 1.

TIER 2, SAFETY (not recoverable: hard block, forced escalation)
  4. ALLERGEN ABSENCE. Any statement that an item is free of, safe for, or
     suitable for an allergy, or that it lacks an ingredient. Blocked even when
     true and even when check_dietary_info ran: a caller hears the claim, not the
     attribution, and a menu flag is not a safety judgment.
  5. AUTONOMOUS ALLERGY COMMITMENT. Once an allergy has been declared, the
     assistant may not present a hold as confirmed without a human in the loop.

Why Tier 2 never retries: a regenerated answer that happens to pass validation
is still a model guessing about anaphylaxis. Randomness is not a safety
mechanism. The agent routes Tier 2 straight to the escalation script.

ASSERTIONS versus PROPOSALS. An assertion's truth is settled by a tool result;
a proposal's is settled by the action that follows it. "We have eleven" must
match a tool result exactly. "Want me to set one aside?" is an offer whose truth
is settled by create_hold, which refuses what it cannot honour. Without this
split the assistant could never offer to do anything. Proposals are bounded: the
offered quantity cannot exceed the last known availability, and under stale
data no specific number is permitted.

FALSE POSITIVES this module must not raise, each with a test: times, prices,
phone numbers, dates, hold ids, addresses, durations, sizes (9-inch), idioms
("one moment"), and the caller's own numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from holdline.state import ConversationState
from holdline.tools import ToolResult

# --- Number vocabulary -----------------------------------------------------------------

NUMBER_WORDS: dict[str, int] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    "hundred": 100,
}
PHRASE_NUMBERS: list[tuple[str, int]] = [
    (r"half a dozen", 6),
    (r"a dozen", 12),
    (r"dozen", 12),
    (r"a couple of", 2),
    (r"a couple", 2),
    (r"a pair of", 2),
]

_WORD_ALTERNATION = "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))
NUMBER_RE = re.compile(
    r"(?P<phrase>" + "|".join(p for p, _ in PHRASE_NUMBERS) + r")"
    r"|(?P<word>\b(?:" + _WORD_ALTERNATION + r")\b(?:[- ](?:" + _WORD_ALTERNATION + r")\b)?)"
    r"|(?P<digits>\b\d+(?:\.\d+)?\b)",
    re.IGNORECASE,
)

# Text removed before scanning: a phone number is never a quantity.
PHONE_RE = re.compile(r"\(?\b\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b|\b\d{5,}\b")
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s*(?:am|pm|a\.m\.|p\.m\.)?", re.IGNORECASE)

TIME_PREPOSITIONS = {"at", "by", "until", "till", "before", "after", "past", "to", "for"}
# "around five" is a time when the sentence is about coming by; "around 8" with no such
# cue is a hedged quantity claim and must be grounded.
APPROX_PREPOSITIONS = {"around", "about", "roughly", "approximately"}
TIME_CUE_RE = re.compile(
    r"\b(?:come|coming|pick(?:ing)?\s?up|pickup|collect|be (?:there|by|in|here)|swing|stop|arrive|"
    r"arriving|open|close|closing|ready|see you|back|here|drop|get here|make it)\b",
    re.IGNORECASE,
)
TIME_SUFFIXES = re.compile(
    r"^\s*(?:am|pm|a\.m\.|p\.m\.|o'?clock|ish|-ish|thirty|fifteen|forty-five|tonight|today|this)\b",
    re.IGNORECASE,
)
STOCK_NOUNS = re.compile(
    r"^\s*(?:more\s+)?(?:\w+\s+){0,2}?(?:cakes?|loa(?:f|ves)|croissants?|cookies?|brownies?|"
    r"baguettes?|cheesecakes?|lattes?|cold brews?|coffees?|items?|units?|pieces?|slices?|"
    r"left|available|in stock|on hand|of (?:them|those|the)|remaining|ready|sellable)\b",
    re.IGNORECASE,
)
DURATION_SUFFIX = re.compile(
    r"^\s*(?:more\s+)?(?:minutes?|mins?|hours?|hrs?|seconds?|secs?|days?|weeks?|months?)\b",
    re.IGNORECASE,
)
SIZE_SUFFIX = re.compile(
    r'^\s*(?:-?\s?inch(?:es)?|"|in\b|lbs?\b|pounds?|oz\b|ounces?|cm\b|%)', re.IGNORECASE
)
MONEY_SUFFIX = re.compile(r"^\s*(?:dollars?|bucks?|cents?|usd)\b", re.IGNORECASE)
ORDINAL_SUFFIX = re.compile(r"^(?:st|nd|rd|th)\b")
DATE_PREFIX = re.compile(
    r"(?:january|february|march|april|may|june|july|august|september|october|november|december|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\.?\s*$",
    re.IGNORECASE,
)
ID_PREFIX = re.compile(
    r"(?:#|\b(?:hold|order|reference|confirmation|ticket)\s+(?:number|no\.?|id|#)|\bref|\bnumber|\bno\.|\bid)"
    r"\s*(?:is|:|was|')?\s*$",
    re.IGNORECASE,
)
ADDRESS_SUFFIX = re.compile(
    r"^\s*(?:[NSEW]\.?\s+|(?i:north|south|east|west)\s+)?[A-Z][a-z]+\s+"
    r"(?i:st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|ln|lane)\b",
)
ONE_IDIOM_PREFIX = re.compile(
    r"\b(?:which|that|this|the|another|each|any|no|some|every|only|other|chocolate|lemon|big|small|"
    r"little|right|wrong|same|first|last|next|good|better|best)\s*$",
    re.IGNORECASE,
)
ONE_IDIOM_SUFFIX = re.compile(
    r"^\s*(?:moment|second|sec|minute|of|more thing|thing|way|day)\b", re.IGNORECASE
)
PRONOUN_ONE_SUFFIX = re.compile(r"^\s*(?:aside|back|for you|under|on hold|ready)\b", re.IGNORECASE)

# --- Sentence-level patterns ------------------------------------------------------------

PROPOSAL_RE = re.compile(
    r"\b(?:want me to|would you like|shall i|should i|can i (?:set|hold|put|reserve|grab|save)|"
    r"could i (?:set|hold|put|reserve)|how about|set (?:\w+\s+){0,3}?aside|put (?:\w+\s+){0,3}?aside|"
    r"hold (?:\w+\s+){0,3}?for you|save (?:\w+\s+){0,3}?for you|reserve|happy to (?:set|hold|put))\b",
    re.IGNORECASE,
)
CONFIRMATION_RE = re.compile(
    r"\b(?:i'?ve (?:set|put) (?:\w+\s+){0,3}?aside|(?:is|are|it's|they're) set aside|"
    r"(?:i've |i have |we've |we have )?(?:got|have) (?:you|that|it|them|those|one|two|\w+) (?:down|on hold|reserved|set aside)|"
    r"you'?re all set|all set|(?:is|are|it's|they're|that's) (?:on hold|reserved|held|yours)|"
    r"i'?ve (?:held|reserved) |i have (?:held|reserved) |(?:holding|reserving) (?:\w+\s+){0,3}?for you|"
    r"under your name|we'?ll have (?:it|them|that|those) (?:ready|waiting)|"
    r"(?:is|are) (?:confirmed|booked|guaranteed)|consider it (?:done|held|reserved)|"
    r"(?:it's|that's|you're) (?:confirmed|booked|guaranteed))\b",
    re.IGNORECASE,
)
HANDOFF_RE = re.compile(
    r"\b(?:call (?:you )?(?:right )?back|someone (?:will|from|here)|a person|person will|"
    r"confirm (?:with|that|it|this)|check with|double[- ]check|pending|approval|approve|"
    r"get back to you|have (?:someone|the kitchen|a (?:staff|team) member|the manager)|"
    r"soft hold|needs? a (?:quick )?(?:ok|okay|sign[- ]off)|manager|staff)\b",
    re.IGNORECASE,
)
GUARANTEE_RE = re.compile(
    r"\b(?:you'?re all set|all set|(?:is|are|it's|that's) (?:confirmed|guaranteed|booked|yours)|"
    r"consider it done|(?:it's|that's) (?:on hold|reserved)|i promise|guaranteed|definitely (?:held|reserved))\b",
    re.IGNORECASE,
)

ALLERGEN_WORDS = (
    r"nut|nuts|peanut|peanuts|tree[- ]nuts?|almonds?|walnuts?|hazelnuts?|pecans?|cashews?|"
    r"gluten|wheat|dairy|milk|lactose|butter|eggs?|soy|soya|sesame|shellfish|fish|allergens?"
)
ABSENCE_PATTERNS = [
    rf"\b(?:{ALLERGEN_WORDS})[- ]free\b",
    r"\bsafe (?:for|to eat|to have|for (?:you|her|him|them|your))\b",
    r"\bsuitable for\b",
    r"\b(?:fine|ok|okay|good|alright) for (?:you|her|him|them|your|someone|people|anyone|a)\b",
    rf"\b(?:doesn'?t|does not|don'?t|do not|won'?t|will not|didn'?t) (?:contain|have|include|use) (?:any )?(?:{ALLERGEN_WORDS})\b",
    rf"\bno (?:{ALLERGEN_WORDS}) (?:in|at all|whatsoever|inside)\b",
    rf"\b(?:there'?s|there is|there are) no (?:{ALLERGEN_WORDS})\b",
    rf"\bfree (?:of|from) (?:any )?(?:{ALLERGEN_WORDS})\b",
    r"\b(?:shouldn'?t|should not|won'?t|will not) be (?:a problem|an issue|any (?:problem|issue|trouble))\b",
    r"\bshould be (?:fine|ok|okay|safe|alright|totally fine)\b",
    r"\b(?:is|it'?s|its|that'?s|they'?re|are|be) (?:totally |completely |perfectly |definitely |absolutely |100% )?(?:fine|safe|ok|okay) (?:for|to)\b",
    r"\b(?:is|it'?s|that'?s|they'?re|are|be|be a) (?:totally |completely |fully |100% )?(?:vegan|vegetarian|gluten[- ]free|dairy[- ]free|nut[- ]free|kosher|halal)(?: option| choice| item)?\b",
    r"\bnothing (?:in (?:it|there|that|them) )?(?:to|you(?:'d|'ll| would| should| need to)?|she|he|they)(?:'d| would| should)? ?(?:need to |have to )?worry about\b",
    r"\bno (?:allergens|cross[- ]contamination|cross[- ]contact)\b",
    rf"\b(?:isn'?t|is not|aren'?t|are not|not) made with (?:any )?(?:{ALLERGEN_WORDS})\b",
    rf"\bwithout (?:any )?(?:{ALLERGEN_WORDS})\b",
    r"\bwon'?t (?:trigger|cause|set off|affect)\b",
]
ABSENCE_RE = re.compile("|".join(f"(?:{p})" for p in ABSENCE_PATTERNS), re.IGNORECASE)

# A disclaimer window: "I can't say it's nut-free" is the correct behaviour.
DISCLAIMER_RE = re.compile(
    r"\b(?:can'?t|cannot|can not|not able to|unable to|won'?t|will not|never|don'?t|do not|"
    r"i'?m not (?:able|going|allowed)|not going to|no way|wouldn'?t|couldn'?t|shouldn'?t|"
    r"not (?:say|promise|guarantee|confirm|claim|tell|able|allowed|certain|sure|comfortable)|"
    r"rather than|instead of|whether|if it'?s|if (?:that|this|it) (?:is|would be)|"
    r"not (?:something|a (?:call|judgment|promise)))\b",
    re.IGNORECASE,
)
DISCLAIMER_WINDOW_WORDS = 9

PRESENCE_RE = re.compile(
    rf"\b(?:contains?|has|have|made with|includes?|there'?s|there is|there are|got|uses?|with)\b"
    rf"(?:\s+(?:some|a bit of|a little|real|fresh|both|also|and|,|or|the|a|an|its|their|no))*"
    rf"\s+(?P<allergen>{ALLERGEN_WORDS})\b",
    re.IGNORECASE,
)
ALLERGEN_CANON: dict[str, str] = {
    "nut": "tree nuts",
    "nuts": "tree nuts",
    "tree nut": "tree nuts",
    "tree nuts": "tree nuts",
    "tree-nut": "tree nuts",
    "tree-nuts": "tree nuts",
    "almond": "tree nuts",
    "almonds": "tree nuts",
    "walnut": "tree nuts",
    "walnuts": "tree nuts",
    "hazelnut": "tree nuts",
    "hazelnuts": "tree nuts",
    "pecan": "tree nuts",
    "pecans": "tree nuts",
    "cashew": "tree nuts",
    "cashews": "tree nuts",
    "peanut": "peanuts",
    "peanuts": "peanuts",
    "gluten": "wheat",
    "wheat": "wheat",
    "dairy": "dairy",
    "milk": "dairy",
    "lactose": "dairy",
    "butter": "dairy",
    "egg": "eggs",
    "eggs": "eggs",
    "soy": "soy",
    "soya": "soy",
    "sesame": "sesame",
    "shellfish": "shellfish",
    "fish": "fish",
    "allergen": "allergens",
    "allergens": "allergens",
}


# --- Data types -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    rule: str
    tier: int
    excerpt: str

    def to_dict(self) -> dict:
        return {"rule": self.rule, "tier": self.tier, "excerpt": self.excerpt}


@dataclass
class ValidationResult:
    violations: list[Violation] = field(default_factory=list)

    @property
    def tier(self) -> int:
        """0 when clean, else the highest tier violated."""
        return max((v.tier for v in self.violations), default=0)

    @property
    def ok(self) -> bool:
        return not self.violations


@dataclass(frozen=True)
class QuantityMention:
    """A number in the text that survived every false-positive filter."""

    value: int
    excerpt: str
    sentence: str
    is_proposal: bool


# --- Extraction --------------------------------------------------------------------------------


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p]


def _strip_non_quantities(text: str) -> str:
    """Blank out phone numbers and clock times so they are never scanned."""
    text = PHONE_RE.sub(lambda m: " " * len(m.group(0)), text)
    return TIME_RE.sub(lambda m: " " * len(m.group(0)), text)


def _word_value(token: str) -> int | None:
    parts = re.split(r"[- ]", token.lower())
    total = 0
    for part in parts:
        if part not in NUMBER_WORDS:
            return None
        total += NUMBER_WORDS[part]
    return total


def _is_false_positive(text: str, start: int, end: int, raw: str, value: int) -> bool:
    before = text[:start]
    after = text[end:]
    before_words = before.rstrip().split()
    prev = before_words[-1].lower().strip(",") if before_words else ""

    if before.rstrip().endswith("$") or MONEY_SUFFIX.match(after):
        return True
    if "." in raw:  # 3.50 is a price or a measurement, never a count
        return True
    if TIME_SUFFIXES.match(after) or ORDINAL_SUFFIX.match(after):
        return True
    if DURATION_SUFFIX.match(after) or SIZE_SUFFIX.match(after) or ADDRESS_SUFFIX.match(after):
        return True
    if DATE_PREFIX.search(before) or ID_PREFIX.search(before):
        return True
    if raw.isdigit() and len(raw) == 4 and 1900 <= value <= 2100:  # a year
        return True
    if after.lstrip().startswith("/") or before.rstrip().endswith("/"):
        return True
    # 'at five' is a time; 'about a dozen' never is, so phrase numbers skip this rule.
    is_phrase = not raw.isdigit() and raw.lower() not in NUMBER_WORDS and "-" not in raw
    if prev in TIME_PREPOSITIONS and value <= 12 and not is_phrase and not STOCK_NOUNS.match(after):
        return True
    if (
        prev in APPROX_PREPOSITIONS
        and value <= 12
        and not is_phrase
        and not STOCK_NOUNS.match(after)
    ):
        sentence_so_far = text[_sentence_start(text, start) : start]
        if TIME_CUE_RE.search(sentence_so_far):
            return True
    if raw.lower() == "one" and (ONE_IDIOM_PREFIX.search(before) or ONE_IDIOM_SUFFIX.match(after)):
        return True
    if raw.lower() in {"one", "two"} and PRONOUN_ONE_SUFFIX.match(after):
        # "set one aside", "hold two for you": the number IS a proposal, keep it.
        return False
    return False


def extract_quantities(text: str) -> list[QuantityMention]:
    """Every quantity claim in the text, classified as assertion or proposal."""
    cleaned = _strip_non_quantities(text)
    mentions: list[QuantityMention] = []
    for match in NUMBER_RE.finditer(cleaned):
        raw = match.group(0)
        if match.group("phrase"):
            value = next(v for p, v in PHRASE_NUMBERS if re.fullmatch(p, raw, re.IGNORECASE))
        elif match.group("word"):
            maybe = _word_value(raw)
            if maybe is None:
                continue
            value = maybe
        else:
            value = int(float(raw))
        if _is_false_positive(cleaned, match.start(), match.end(), raw, value):
            continue
        sentence = _sentence_containing(cleaned, match.start())
        is_proposal = sentence.strip().endswith("?") or bool(PROPOSAL_RE.search(sentence))
        mentions.append(QuantityMention(value, raw, sentence.strip(), is_proposal))
    return mentions


def _sentence_start(text: str, index: int) -> int:
    start = max(text.rfind(". ", 0, index), text.rfind("! ", 0, index), text.rfind("? ", 0, index))
    return 0 if start == -1 else start + 2


def _sentence_containing(text: str, index: int) -> str:
    start = _sentence_start(text, index)
    ends = [
        i
        for i in (text.find(". ", index), text.find("! ", index), text.find("? ", index))
        if i != -1
    ]
    end = min(ends) + 1 if ends else len(text)
    return text[start:end]


def customer_numbers(text: str) -> set[int]:
    """Numbers the caller said. No false-positive filtering: if the caller said
    'two' in any sense, the assistant echoing 'two' is not an invention."""
    values: set[int] = set()
    for match in NUMBER_RE.finditer(text):
        raw = match.group(0)
        if match.group("phrase"):
            values.add(next(v for p, v in PHRASE_NUMBERS if re.fullmatch(p, raw, re.IGNORECASE)))
        elif match.group("word"):
            maybe = _word_value(raw)
            if maybe is not None:
                values.add(maybe)
        else:
            values.add(int(float(raw)))
    return values


# --- Grounding set from tool results --------------------------------------------------------


def _numbers_in(obj: object, out: set[int]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, int):
        out.add(obj)
    elif isinstance(obj, float) and obj.is_integer():
        out.add(int(obj))
    elif isinstance(obj, str):
        if obj.isdigit():
            out.add(int(obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k in {"hold_id", "age_seconds", "as_of", "expires_at", "price", "phone", "hours"}:
                continue  # ids and timestamps are not quantities the model may quote
            _numbers_in(v, out)
    elif isinstance(obj, list | tuple):
        for v in obj:
            _numbers_in(v, out)


def grounding_numbers(tool_results: list[ToolResult], state: ConversationState) -> set[int]:
    """Numbers the assistant may state: tool outputs, tool inputs, the caller's own."""
    out: set[int] = set()
    for result in tool_results:
        _numbers_in(result.output, out)
        if result.name == "create_hold":
            _numbers_in(result.input.get("quantity"), out)
    out |= state.customer_numbers
    return out


def known_quantities(tool_results: list[ToolResult]) -> list[int]:
    return [
        int(r.output["quantity"])
        for r in tool_results
        if r.name == "check_availability" and r.output.get("status") == "known"
    ]


def availability_unknown_this_turn(tool_results: list[ToolResult]) -> bool:
    return any(
        r.name == "check_availability" and r.output.get("status") == "unknown" for r in tool_results
    )


def successful_holds(tool_results: list[ToolResult]) -> list[dict]:
    return [r.output for r in tool_results if r.name == "create_hold" and r.output.get("ok")]


# --- The validator --------------------------------------------------------------------------


def _has_disclaimer(sentence: str, match_start: int) -> bool:
    window = " ".join(sentence[:match_start].split()[-DISCLAIMER_WINDOW_WORDS:])
    return bool(DISCLAIMER_RE.search(window))


def validate_response(
    text: str,
    tool_results_this_turn: list[ToolResult],
    state: ConversationState,
) -> ValidationResult:
    """Judge one candidate reply. Pure: no I/O, no state mutation."""
    result = ValidationResult()
    if not text.strip():
        return result

    # ---- Tier 2 first: a safety block makes the grounding findings moot. ----
    for sentence in _sentences(text):
        for match in ABSENCE_RE.finditer(sentence):
            if _has_disclaimer(sentence, match.start()):
                continue
            result.violations.append(Violation("allergen_absence_claim", 2, sentence.strip()))
            break

    allergy_declared = state.dietary_severity == "allergy"
    holds_ok = successful_holds(tool_results_this_turn)
    if allergy_declared:
        if any(h.get("status") == "active" for h in holds_ok):
            result.violations.append(
                Violation(
                    "allergy_hold_activated",
                    2,
                    "create_hold returned active despite a declared allergy",
                )
            )
        for sentence in _sentences(text):
            if CONFIRMATION_RE.search(sentence) and not HANDOFF_RE.search(text):
                result.violations.append(
                    Violation("autonomous_allergy_commitment", 2, sentence.strip())
                )
                break

    # ---- Tier 1 ----
    mentions = extract_quantities(text)
    grounded = grounding_numbers(tool_results_this_turn, state)
    known_now = known_quantities(tool_results_this_turn)
    ceiling = (
        max(known_now)
        if known_now
        else (max(state.last_known_quantities.values()) if state.last_known_quantities else None)
    )
    stale = availability_unknown_this_turn(tool_results_this_turn) or not state.availability_known

    for mention in mentions:
        if stale and not known_now:
            result.violations.append(Violation("stale_quantity", 1, mention.sentence))
            continue
        if mention.is_proposal:
            if ceiling is None or mention.value > ceiling:
                result.violations.append(
                    Violation("proposal_exceeds_availability", 1, mention.sentence)
                )
            continue
        if mention.value not in grounded:
            result.violations.append(Violation("ungrounded_quantity", 1, mention.sentence))

    for sentence in _sentences(text):
        if sentence.strip().endswith("?"):
            continue
        if CONFIRMATION_RE.search(sentence):
            if not holds_ok:
                result.violations.append(Violation("unconfirmed_hold", 1, sentence.strip()))
            elif (
                all(h.get("status") == "pending_approval" for h in holds_ok)
                and GUARANTEE_RE.search(sentence)
                and not HANDOFF_RE.search(sentence)
            ):
                result.violations.append(
                    Violation("pending_presented_as_confirmed", 1, sentence.strip())
                )

    declared = set()
    for r in tool_results_this_turn:
        if r.name == "check_dietary_info" and r.ok:
            for word in r.output.get("contains", []) + r.output.get("may_contain", []):
                declared.add(ALLERGEN_CANON.get(word.lower(), word.lower()))
    for sentence in _sentences(text):
        for match in PRESENCE_RE.finditer(sentence):
            allergen = ALLERGEN_CANON.get(
                match.group("allergen").lower(), match.group("allergen").lower()
            )
            if _has_disclaimer(sentence, match.start()):
                continue
            if allergen == "allergens":
                continue
            if allergen not in declared:
                result.violations.append(Violation("ungrounded_presence", 1, sentence.strip()))
                break

    return result
