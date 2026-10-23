"""Seeding must be idempotent, and the catalogue must contain the hard cases."""

from __future__ import annotations

import json
from pathlib import Path

from holdline import db
from holdline.seed import seed
from holdline.seed_data import ITEMS, LOCATION


def _catalogue_snapshot() -> list[tuple[object, ...]]:
    """Return every item and stock row as comparable tuples."""
    rows = db.query(
        """
        SELECT i.id, i.name, i.price_cents, i.safety_buffer,
               i.contains, i.may_contain, i.dietary_flags, s.on_hand
        FROM items i
        JOIN stock s ON s.item_id = i.id
        ORDER BY i.id
        """
    )
    return [tuple(row) for row in rows]


def test_seeding_produces_one_location_and_twelve_items(seeded_db: Path) -> None:
    """The demo runs on one location and twelve items."""
    assert len(db.query("SELECT id FROM locations")) == 1
    assert len(db.query("SELECT id FROM items")) == 12
    assert len(db.query("SELECT item_id FROM stock")) == 12


def test_seeding_twice_leaves_exactly_the_same_state(seeded_db: Path) -> None:
    """Running the seeder again must not duplicate rows or drift the catalogue."""
    before = _catalogue_snapshot()

    seed()

    assert _catalogue_snapshot() == before
    assert len(db.query("SELECT id FROM locations")) == 1


def test_reseeding_rewinds_simulated_sales_to_the_baseline(seeded_db: Path) -> None:
    """Re-seeding is how a demo gets rewound, so it must undo stock changes."""
    with db.transaction(immediate=True) as conn:
        conn.execute(
            "UPDATE stock SET on_hand = 0 WHERE item_id = ?",
            ("cake_chocolate_9",),
        )

    seed()

    row = db.query_one("SELECT on_hand FROM stock WHERE item_id = ?", ("cake_chocolate_9",))
    assert row is not None
    assert row["on_hand"] == 12


def test_seeding_never_touches_the_ledger_or_the_audit_trail(seeded_db: Path) -> None:
    """Holds, alerts and events are evidence. Only an explicit reset may clear them."""
    with db.transaction(immediate=True) as conn:
        conn.execute(
            "INSERT INTO events (at, location_id, kind, detail) VALUES (?, ?, ?, ?)",
            ("2026-09-29T21:00:00Z", LOCATION.id, "call_started", "{}"),
        )

    seed()

    assert len(db.query("SELECT id FROM events")) == 1


def test_every_item_declares_allergens_as_valid_json_arrays(seeded_db: Path) -> None:
    """The dietary tool reads these fields verbatim, so malformed JSON is a safety bug."""
    for row in db.query("SELECT id, contains, may_contain, dietary_flags FROM items"):
        for column in ("contains", "may_contain", "dietary_flags"):
            parsed = json.loads(row[column])
            assert isinstance(parsed, list), f"{row['id']}.{column} is not a JSON array"
            assert all(isinstance(entry, str) for entry in parsed)


def test_eleven_chocolate_cakes_are_sellable_before_any_holds(seeded_db: Path) -> None:
    """The README transcript quotes eleven, so the seed data must actually say eleven.

    Phase 2 computes this properly. Here it is asserted arithmetically -- on_hand
    minus safety buffer -- so that a later change to the seed data cannot quietly
    make the documented demo a lie.
    """
    row = db.query_one(
        """
        SELECT s.on_hand, i.safety_buffer
        FROM stock s JOIN items i ON i.id = s.item_id
        WHERE s.item_id = ?
        """,
        ("cake_chocolate_9",),
    )
    assert row is not None
    assert row["on_hand"] - row["safety_buffer"] == 11


def test_the_gluten_free_brownie_also_declares_wheat_cross_contact(seeded_db: Path) -> None:
    """The catalogue must contain the case that makes safety judgment unsafe.

    An item flagged `gluten_free` whose `may_contain` includes wheat is not a data
    error -- it is what a shared bench means. Any assistant that reasoned from the
    marketing flag to "safe for coeliac disease" would be confidently wrong, which
    is why `dietary.py` never reasons and always escalates.
    """
    row = db.query_one(
        "SELECT dietary_flags, may_contain FROM items WHERE id = ?",
        ("pastry_flourless_brownie",),
    )
    assert row is not None
    assert "gluten_free" in json.loads(row["dietary_flags"])
    assert "wheat" in json.loads(row["may_contain"])


def test_nut_content_in_the_batter_is_declared_as_contains_not_may_contain(
    seeded_db: Path,
) -> None:
    """Walnuts baked into carrot cake are an ingredient, not a cross-contact risk.

    Conflating the two fields would understate a real allergen, so the distinction
    is asserted on the item where it is easiest to get wrong.
    """
    row = db.query_one("SELECT contains, may_contain FROM items WHERE id = ?", ("cake_carrot_9",))
    assert row is not None
    assert "tree nuts" in json.loads(row["contains"])
    assert "tree nuts" not in json.loads(row["may_contain"])


def test_made_to_order_drinks_carry_no_safety_buffer(seeded_db: Path) -> None:
    """A buffer exists because the POS lags the counter; a latte made on request does not."""
    rows = db.query("SELECT id, safety_buffer FROM items WHERE category = 'drinks'")

    assert rows
    assert all(row["safety_buffer"] == 0 for row in rows)


def test_the_seed_catalogue_matches_the_module_that_declares_it(seeded_db: Path) -> None:
    """Guards against a loader that silently drops or reorders items."""
    stored = {row["id"] for row in db.query("SELECT id FROM items")}

    assert stored == {item.id for item in ITEMS}
