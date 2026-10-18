"""Seed one bakery location with twelve items and realistic allergen data.

Idempotent: running it twice leaves the same state (INSERT OR REPLACE on the
catalogue, stock reset to the seed quantities).

The catalogue deliberately includes a trap for a careless assistant: the
flourless brownie carries a "gluten-free" dietary flag AND declares wheat as a
may_contain cross-contact risk. A flag is not a safety claim. See dietary.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from holdline import clock, events
from holdline.config import get_settings
from holdline.db import Database
from holdline.logging_setup import configure, get_logger

log = get_logger(__name__)

LOCATION_ID = "marchettis_lincoln_park"

LOCATION = {
    "id": LOCATION_ID,
    "name": "Marchetti's Bakery, Lincoln Park",
    "timezone": "America/Chicago",
    "phone": "(312) 555-0142",
    "address": "2140 N Halsted St, Chicago, IL 60614",
    "hours": "Tuesday to Sunday 7am to 7pm, closed Monday",
}

# (id, name, category, price_cents, safety_buffer, contains, may_contain, flags, seed_stock)
ITEMS: list[tuple[str, str, str, int, int, list[str], list[str], list[str], int]] = [
    (
        "cake_choc_9",
        'Chocolate Cake 9"',
        "cake",
        3800,
        1,
        ["wheat", "eggs", "dairy", "soy"],
        ["tree nuts"],
        [],
        11,
    ),
    (
        "cake_carrot",
        "Carrot Cake",
        "cake",
        3600,
        1,
        ["wheat", "eggs", "dairy", "tree nuts"],
        [],
        [],
        4,
    ),
    (
        "loaf_lemon",
        "Lemon Drizzle Loaf",
        "cake",
        2200,
        1,
        ["wheat", "eggs", "dairy"],
        ["tree nuts"],
        [],
        6,
    ),
    (
        "cheesecake_ny",
        "New York Cheesecake",
        "cake",
        4200,
        1,
        ["wheat", "eggs", "dairy"],
        [],
        [],
        3,
    ),
    (
        "croissant_butter",
        "Butter Croissant",
        "pastry",
        425,
        2,
        ["wheat", "dairy", "eggs"],
        ["tree nuts"],
        [],
        24,
    ),
    (
        "croissant_almond",
        "Almond Croissant",
        "pastry",
        495,
        2,
        ["wheat", "dairy", "eggs", "tree nuts"],
        [],
        [],
        9,
    ),
    (
        "cookie_pb",
        "Peanut Butter Cookie",
        "pastry",
        350,
        2,
        ["wheat", "eggs", "dairy", "peanuts"],
        ["tree nuts"],
        [],
        18,
    ),
    (
        "brownie_flourless",
        "Flourless Chocolate Brownie",
        "pastry",
        450,
        1,
        ["eggs", "dairy", "soy"],
        ["wheat", "tree nuts"],
        ["gluten-free"],
        7,
    ),
    ("bread_sourdough", "Sourdough Loaf", "bread", 900, 1, ["wheat"], [], ["vegan"], 8),
    ("bread_baguette", "Baguette", "bread", 450, 1, ["wheat"], ["sesame"], ["vegan"], 15),
    ("drink_cold_brew", "Cold Brew Coffee", "drink", 475, 0, [], [], ["vegan"], 30),
    ("drink_oat_latte", "Oat Milk Latte", "drink", 550, 0, ["oats"], ["dairy"], ["vegan"], 30),
]


def seed(db: Database, *, reset_events: bool = False) -> None:
    """Write the catalogue and stock. Safe to run repeatedly."""
    db.apply_schema()
    with db.transaction(immediate=True) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO locations (id, name, timezone, phone, address, hours)"
            " VALUES (:id, :name, :timezone, :phone, :address, :hours)",
            LOCATION,
        )
        now = clock.iso()
        for item_id, name, category, price, buffer, contains, may, flags, qty in ITEMS:
            conn.execute(
                "INSERT OR REPLACE INTO items"
                " (id, name, category, price_cents, safety_buffer,"
                "  contains, may_contain, dietary_flags)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item_id,
                    name,
                    category,
                    price,
                    buffer,
                    json.dumps(contains),
                    json.dumps(may),
                    json.dumps(flags),
                ),
            )
            conn.execute(
                "INSERT OR REPLACE INTO stock (location_id, item_id, on_hand, last_synced_at)"
                " VALUES (?, ?, ?, ?)",
                (LOCATION_ID, item_id, qty, now),
            )
        # Holds and alerts from a previous demo would otherwise distort availability.
        conn.execute("DELETE FROM holds")
        conn.execute("DELETE FROM staff_alerts")
        conn.execute("DELETE FROM deliveries")
        if reset_events:
            conn.execute("DELETE FROM events")
        events.record(conn, "seed", {"items": len(ITEMS)}, location_id=LOCATION_ID, channel="pos")
    log.info("seeded", items=len(ITEMS), db=str(db.path))


def main(argv: list[str] | None = None) -> int:
    """CLI entry: `python -m holdline.seed [path]`."""
    settings = get_settings()
    configure(settings.log_level)
    args = argv if argv is not None else sys.argv[1:]
    path = Path(args[0]) if args else settings.db_path
    seed(Database(path), reset_events=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
