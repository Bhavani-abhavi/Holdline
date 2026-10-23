"""The seven tools, defined once as data and dispatched from one place.

Policy lives in the tool implementations, not in the prompt: `create_hold`
decides pending_approval versus active and refuses impossible requests;
`list_menu` never returns quantities; `check_dietary_info` relays declarations
and judges nothing.

The schemas in TOOL_SCHEMAS are the single source of truth. The browser agent
sends them to the model; the phone layer (Phase 6) registers them with the voice
platform from the same list; nobody hand-maintains a second copy that can drift.
"""

from __future__ import annotations

import difflib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from holdline import clock, config, events, holds
from holdline.availability import sellable
from holdline.db import Database
from holdline.dietary import check_dietary_info
from holdline.exceptions import UnknownItemError
from holdline.logging_setup import get_logger, phone_fingerprint
from holdline.notifications import fan_out

log = get_logger(__name__)

# Spoken names that do not appear in the catalogue text. Kept as data so a new
# item is a row here and a row in seed.py, not code.
ALIASES: dict[str, list[str]] = {
    "cake_choc_9": ["chocolate cake", "choc cake", "nine inch chocolate cake"],
    "cake_carrot": ["carrot cake", "carrot"],
    "loaf_lemon": ["lemon loaf", "lemon drizzle", "lemon cake", "lemon"],
    "cheesecake_ny": ["cheesecake", "new york cheesecake"],
    "croissant_butter": ["croissant", "plain croissant", "butter croissant"],
    "croissant_almond": ["almond croissant"],
    "cookie_pb": ["peanut butter cookie", "pb cookie", "cookie", "cookies"],
    "brownie_flourless": ["brownie", "brownies", "flourless brownie", "chocolate brownie"],
    "bread_sourdough": ["sourdough", "sourdough loaf", "bread"],
    "bread_baguette": ["baguette", "french bread"],
    "drink_cold_brew": ["cold brew", "coffee", "iced coffee"],
    "drink_oat_latte": ["oat latte", "oat milk latte", "latte"],
}

# A match this far ahead of the runner-up is unambiguous. 0.15 was chosen so
# "chocolate cake" beats "chocolate brownie" cleanly while "the chocolate one"
# (which matches both equally) comes back as ambiguous.
MATCH_MARGIN = 0.15
MATCH_FLOOR = 0.45
# Two tokens this similar are the same word mistyped ('chocolat' vs 'chocolate').
TOKEN_TYPO_RATIO = 0.8


@dataclass(frozen=True)
class ToolContext:
    """Who is calling, from where. Threaded into every event the tools write."""

    location_id: str
    call_id: str
    channel: str


@dataclass(frozen=True)
class ToolResult:
    """One executed tool call, exactly as the guardrail and the glass box see it."""

    name: str
    input: dict[str, Any]
    output: dict[str, Any]
    ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "arguments": self.input, "result": self.output, "ok": self.ok}


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "check_availability",
        "description": (
            "Look up how many of an item can be promised right now. Pass the caller's own words "
            "for the item. Returns status 'known' with a quantity, 'unknown' when the stock data "
            "is stale (then say you are not certain and offer a callback; never guess a number), "
            "'ambiguous' with candidates, or 'no_match'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_query": {"type": "string", "description": "What the caller said."}
            },
            "required": ["item_query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_menu",
        "description": "Names, categories and prices of everything on the menu. Never includes quantities.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "check_dietary_info",
        "description": (
            "Declared ingredients for an item: contains, may_contain (shared equipment), and "
            "menu flags. Relay these words to the caller. Never conclude that an item is safe, "
            "free of, or suitable for an allergy; a person confirms that."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"item_id": {"type": "string"}},
            "required": ["item_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "create_hold",
        "description": (
            "Reserve units of an item for pickup. Always ask about allergies or dietary needs "
            "BEFORE calling this and pass the caller's exact words as dietary_note. The tool "
            "decides whether staff must approve. Only confirm a hold to the caller if ok is true; "
            "if status is pending_approval, say a person will confirm shortly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string", "description": "item_id from check_availability"},
                "quantity": {"type": "integer", "description": "At least 1."},
                "pickup_time": {
                    "type": "string",
                    "description": (
                        "When the caller will collect. Accepts '5pm', '17:30', 'in 45 minutes', "
                        "or ISO 8601. Store local time."
                    ),
                },
                "customer_name": {"type": "string"},
                "customer_phone": {"type": "string"},
                "dietary_note": {
                    "type": "string",
                    "description": "The caller's own words about allergies or dietary needs, verbatim.",
                },
            },
            "required": ["item_id", "quantity", "pickup_time"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cancel_hold",
        "description": "Cancel a hold the caller made earlier in this call.",
        "input_schema": {
            "type": "object",
            "properties": {"hold_id": {"type": "integer"}},
            "required": ["hold_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_store_info",
        "description": "Opening hours, address and phone number of the bakery.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "escalate_to_staff",
        "description": (
            "Hand the caller to a person. Use for declared allergies, requests outside policy, "
            "anything you are unsure about, or when the caller asks for a human."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "callback_number": {"type": "string"},
                "urgency": {"type": "string", "enum": ["info", "action", "urgent"]},
            },
            "required": ["reason", "urgency"],
            "additionalProperties": False,
        },
    },
]

TOOL_NAMES = {schema["name"] for schema in TOOL_SCHEMAS}


# --- Item matching ------------------------------------------------------------------


def _normalise(text: str) -> str:
    text = text.lower().replace('"', " inch ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"\b(the|a|an|some|any|of|your|one|ones|please|do you have|left)\b", " ", text)
    # Singularise so "croissants" and "croissant" count as the same token.
    text = re.sub(r"(\w{4,})s\b", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def _fuzzy_intersection(a: set[str], b: set[str]) -> int:
    """Count tokens of `b` that have a close match in `a` (handles typos like 'chocolat')."""
    count = 0
    for token in b:
        if token in a or any(
            difflib.SequenceMatcher(None, token, other).ratio() >= TOKEN_TYPO_RATIO for other in a
        ):
            count += 1
    return count


def match_item(conn: sqlite3.Connection, query: str) -> tuple[str, list[dict[str, str]]]:
    """Fuzzy-match spoken words to a catalogue item.

    Returns ("match", [item]) for a clear winner, ("ambiguous", candidates) when
    two items score within MATCH_MARGIN, and ("no_match", []) otherwise.
    """
    q = _normalise(query)
    if not q:
        return "no_match", []
    rows = conn.execute("SELECT id, name FROM items").fetchall()
    scored: list[tuple[float, str, str]] = []
    q_tokens = set(q.split())
    for row in rows:
        names = [row["name"], *ALIASES.get(row["id"], [])]
        best = 0.0
        for name in names:
            n = _normalise(name)
            n_tokens = set(n.split())
            matched = _fuzzy_intersection(q_tokens, n_tokens)
            # Jaccard overlap measured against both sides, so a short alias that is
            # merely contained in a long query does not score as a full match.
            overlap = matched / max(1, len(q_tokens) + len(n_tokens) - matched)
            contained = 1.0 if n and (n in q or q in n) else 0.0
            # A longer alias contained in the query beats a shorter one: "almond
            # croissant" must outrank the bare "croissant" alias.
            coverage = min(1.0, len(n_tokens) / max(1, len(q_tokens)))
            best = max(best, 0.6 * overlap + 0.4 * contained * coverage)
        scored.append((best, row["id"], row["name"]))
    scored.sort(reverse=True)
    top = scored[0]
    if top[0] < MATCH_FLOOR:
        return "no_match", []
    close = [s for s in scored if top[0] - s[0] < MATCH_MARGIN and s[0] >= MATCH_FLOOR]
    candidates = [{"item_id": s[1], "item_name": s[2]} for s in close]
    if len(close) > 1:
        return "ambiguous", candidates
    return "match", candidates


# --- Pickup time parsing ------------------------------------------------------------


SPOKEN_NUMBERS = {
    "a": 1,
    "an": 1,
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
    "fifteen": 15,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "forty-five": 45,
    "forty five": 45,
    "fifty": 50,
    "sixty": 60,
    "a couple of": 2,
    "a couple": 2,
    "half an": 0.5,
    "half a": 0.5,
}
ASAP_RE = re.compile(
    r"^(?:now|right (?:now|away)|asap|straight away|on my way|i'?m on my way|immediately)$"
)
SOON_RE = re.compile(
    r"^(?:soon|shortly|in a bit|in a (?:few|couple)(?: of)? (?:minutes|mins)|in a few|a few minutes)$"
)


def _spoken_number(text: str) -> float | None:
    text = text.strip()
    if text.isdigit():
        return float(text)
    return SPOKEN_NUMBERS.get(text)


def _at_local(
    local_now: datetime, hour: int, minute: int, *, days_ahead: int = 0
) -> datetime | None:
    if hour > 23 or minute > 59:
        return None
    target = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return (target + timedelta(days=days_ahead)).astimezone(clock.UTC)


def _bakery_hour(hour: int, suffix: str) -> int:
    """A bakery caller saying "five" means 5pm; "eight" means 8am. The shop opens at 7."""
    if suffix == "pm" and hour < 12:
        return hour + 12
    if suffix == "am" and hour == 12:
        return 0
    if not suffix and hour < 7:
        return hour + 12
    return hour


def parse_pickup(text: str, now: datetime, tz: ZoneInfo) -> datetime | None:
    """Turn what a caller says about pickup into a UTC datetime.

    Understands: "now", "on my way", "soon", "in twenty minutes", "in an hour",
    "in half an hour", "5pm", "5:30 pm", "17:30", "five", "around five",
    "half past four", "quarter to six", "noon", "tomorrow at two", ISO 8601.
    An empty string means the caller is coming shortly. Returns None only when
    the words cannot be understood; the tool then asks rather than guessing.
    """
    t = re.sub(
        r"\s+", " ", text.strip().lower().replace("o'clock", "").replace("o’clock", "")
    ).strip()
    local_now = now.astimezone(tz)
    if not t or SOON_RE.match(t):
        return now + timedelta(minutes=config.DEFAULT_PICKUP_MINUTES)
    if ASAP_RE.match(t):
        return now

    m = re.fullmatch(r"in (?:about |around |like )?(.+?) ?(minutes?|mins?|hours?|hrs?)", t)
    if m:
        n = _spoken_number(m.group(1))
        if n is None:
            return None
        return now + (timedelta(hours=n) if m.group(2).startswith("h") else timedelta(minutes=n))

    days_ahead = 0
    if t.startswith("tomorrow"):
        days_ahead = 1
        t = re.sub(r"^tomorrow,? ?(?:at |around |about )?", "", t).strip() or "noon"
    t = re.sub(r"^(?:at|around|about|by|before) ", "", t)

    if t in {"noon", "12pm", "12 pm", "midday"}:
        return _at_local(local_now, 12, 0, days_ahead=days_ahead)
    m = re.fullmatch(r"(half|quarter) (past|to) (\w+)", t)
    if m:
        base = _spoken_number(m.group(3))
        if base is None:
            return None
        hour = _bakery_hour(int(base), "")
        if m.group(1) == "half":
            return _at_local(local_now, hour, 30, days_ahead=days_ahead)
        if m.group(2) == "past":
            return _at_local(local_now, hour, 15, days_ahead=days_ahead)
        return _at_local(local_now, hour - 1, 45, days_ahead=days_ahead)
    m = re.fullmatch(r"(\d{1,2}|[a-z]+)(?::(\d{2}))? ?(am|pm|a\.m\.|p\.m\.)?(?: ?ish)?", t)
    if m:
        base = _spoken_number(m.group(1))
        if base is None:
            return None
        suffix = (m.group(3) or "").replace(".", "")
        hour = _bakery_hour(int(base), suffix)
        return _at_local(local_now, hour, int(m.group(2) or 0), days_ahead=days_ahead)
    try:
        dt = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(clock.UTC)


# --- Dispatch -------------------------------------------------------------------------


def dispatch(db: Database, ctx: ToolContext, name: str, arguments: dict[str, Any]) -> ToolResult:
    """Run one tool and log it to the event stream. Never raises to the caller:
    an error becomes a result with ok=False so the model can recover."""
    handler = _HANDLERS.get(name)
    if handler is None:
        result = ToolResult(name, arguments, {"error": f"unknown tool {name!r}"}, ok=False)
    else:
        try:
            output = handler(db, ctx, arguments)
            result = ToolResult(name, arguments, output, ok="error" not in output)
        except UnknownItemError as exc:
            result = ToolResult(name, arguments, {"error": f"unknown item {exc}"}, ok=False)
        except (ValueError, TypeError, KeyError) as exc:
            result = ToolResult(name, arguments, {"error": f"bad arguments: {exc}"}, ok=False)
    _log_tool_call(db, ctx, result)
    return result


def _log_tool_call(db: Database, ctx: ToolContext, result: ToolResult) -> None:
    safe_args = dict(result.input)
    if "customer_phone" in safe_args:
        safe_args["customer_phone"] = phone_fingerprint(safe_args["customer_phone"])
    with db.transaction(immediate=True) as conn:
        events.record(
            conn,
            "tool_call",
            {"name": result.name, "arguments": safe_args, "result": result.output, "ok": result.ok},
            location_id=ctx.location_id,
            channel=ctx.channel,
            call_id=ctx.call_id,
        )


def _check_availability(db: Database, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    with db.connection() as conn:
        status, candidates = match_item(conn, str(args.get("item_query", "")))
        if status != "match":
            return {"status": status, "candidates": candidates}
        return sellable(conn, ctx.location_id, candidates[0]["item_id"]).to_dict()


def _list_menu(db: Database, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT id, name, category, price_cents FROM items ORDER BY category, name"
        ).fetchall()
    return {
        "items": [
            {
                "item_id": r["id"],
                "name": r["name"],
                "category": r["category"],
                "price": f"${r['price_cents'] / 100:.2f}",
            }
            for r in rows
        ]
    }


def _check_dietary_info(db: Database, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    with db.connection() as conn:
        return check_dietary_info(conn, str(args["item_id"])).to_dict()


def _store_timezone(db: Database, location_id: str) -> ZoneInfo:
    with db.connection() as conn:
        row = conn.execute("SELECT timezone FROM locations WHERE id = ?", (location_id,)).fetchone()
    return ZoneInfo(row["timezone"] if row else "UTC")


def _create_hold(db: Database, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    pickup = parse_pickup(
        str(args["pickup_time"]), clock.now(), _store_timezone(db, ctx.location_id)
    )
    if pickup is None:
        return {
            "ok": False,
            "reason": "pickup_time_unclear",
            "message": "Ask the caller what time they will come by.",
        }
    result = holds.create_hold(
        db,
        location_id=ctx.location_id,
        item_id=str(args["item_id"]),
        quantity=int(args["quantity"]),
        pickup_at=pickup,
        customer_name=args.get("customer_name"),
        customer_phone=args.get("customer_phone"),
        dietary_note=args.get("dietary_note"),
        call_id=ctx.call_id,
        channel=ctx.channel,
    )
    return result.to_dict()


def _cancel_hold(db: Database, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    ok = holds.cancel_hold(db, int(args["hold_id"]))
    return {"ok": ok} if ok else {"ok": False, "reason": "not_live"}


def _get_store_info(db: Database, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM locations WHERE id = ?", (ctx.location_id,)).fetchone()
    return (
        {k: row[k] for k in ("name", "hours", "address", "phone")}
        if row
        else {"error": "no location"}
    )


def _escalate(db: Database, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    urgency = args.get("urgency", "action")
    payload = {
        "reason": args.get("reason"),
        "callback_number": args.get("callback_number"),
        "call_id": ctx.call_id,
    }
    with db.transaction(immediate=True) as conn:
        fan_out(
            conn,
            ctx.location_id,
            "escalation",
            urgency,
            f"Caller handed to staff: {args.get('reason')}",
            payload,
        )
        events.record(
            conn,
            "escalated",
            {"reason": args.get("reason"), "urgency": urgency},
            location_id=ctx.location_id,
            channel=ctx.channel,
            call_id=ctx.call_id,
        )
    return {"ok": True, "message": "A staff member has been alerted and will follow up."}


_HANDLERS = {
    "check_availability": _check_availability,
    "list_menu": _list_menu,
    "check_dietary_info": _check_dietary_info,
    "create_hold": _create_hold,
    "cancel_hold": _cancel_hold,
    "get_store_info": _get_store_info,
    "escalate_to_staff": _escalate,
}


def result_to_content(result: ToolResult) -> str:
    """Serialise a tool result for the model. Sorted keys keep cache prefixes stable."""
    return json.dumps(result.output, sort_keys=True)
