-- HoldLine schema. One file, no migration framework (deliberate: see docs/adr/0002).
-- All timestamps are ISO 8601 strings in UTC.

CREATE TABLE IF NOT EXISTS locations (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  timezone        TEXT NOT NULL,
  phone           TEXT,
  address         TEXT,
  hours           TEXT NOT NULL DEFAULT ''   -- human-readable opening hours
);

CREATE TABLE IF NOT EXISTS items (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  category        TEXT NOT NULL,             -- cake | pastry | bread | drink
  price_cents     INTEGER NOT NULL,
  safety_buffer   INTEGER NOT NULL DEFAULT 1, -- units never promised over the phone
  contains        TEXT NOT NULL,             -- JSON array: ["wheat","eggs","dairy"]
  may_contain     TEXT NOT NULL,             -- JSON array: shared-equipment risk
  dietary_flags   TEXT NOT NULL              -- JSON array: ["vegetarian"]
);

CREATE TABLE IF NOT EXISTS stock (
  location_id     TEXT NOT NULL REFERENCES locations(id),
  item_id         TEXT NOT NULL REFERENCES items(id),
  on_hand         INTEGER NOT NULL,
  last_synced_at  TEXT NOT NULL,             -- drives the staleness gate in availability.py
  PRIMARY KEY (location_id, item_id)
);

CREATE TABLE IF NOT EXISTS deliveries (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  location_id     TEXT NOT NULL REFERENCES locations(id),
  item_id         TEXT NOT NULL REFERENCES items(id),
  quantity        INTEGER NOT NULL,
  received_at     TEXT NOT NULL,
  note            TEXT
);

CREATE TABLE IF NOT EXISTS holds (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  location_id     TEXT NOT NULL REFERENCES locations(id),
  item_id         TEXT NOT NULL REFERENCES items(id),
  quantity        INTEGER NOT NULL,
  customer_name   TEXT,
  customer_phone  TEXT,
  dietary_note    TEXT,                      -- free text, passed to staff verbatim
  status          TEXT NOT NULL,             -- pending_approval | active | collected
                                             -- | expired | cancelled | declined
  call_id         TEXT,
  channel         TEXT,                      -- browser | phone | staff
  idempotency_key TEXT,                      -- sha256 of (call_id, location, item, qty, pickup minute)
  pickup_at       TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  expires_at      TEXT NOT NULL,
  resolved_at     TEXT
);

-- A replayed create_hold (the model occasionally calls it twice in one turn) must
-- not promise the same cake twice. The partial index only applies while a hold is
-- live, so a customer can legitimately re-order the same thing after collecting.
CREATE UNIQUE INDEX IF NOT EXISTS idx_holds_idempotent
  ON holds(idempotency_key)
  WHERE idempotency_key IS NOT NULL AND status IN ('pending_approval', 'active');

CREATE TABLE IF NOT EXISTS staff_alerts (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  location_id     TEXT NOT NULL REFERENCES locations(id),
  at              TEXT NOT NULL,
  kind            TEXT NOT NULL,             -- new_hold | approval_needed | allergy_callback
                                             -- | hold_expired | guardrail_tier2 | pipeline_failure
  urgency         TEXT NOT NULL,             -- info | action | urgent
  summary         TEXT NOT NULL,
  payload         TEXT NOT NULL,             -- JSON
  acknowledged    INTEGER NOT NULL DEFAULT 0
);

-- events is APPEND-ONLY with a monotonic id. Nothing updates or deletes rows here.
-- That property is what makes watermark-based incremental extraction possible in
-- Phase 8, and would let a streaming source (Kafka) be swapped in later.
CREATE TABLE IF NOT EXISTS events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  at              TEXT NOT NULL,
  location_id     TEXT,
  kind            TEXT NOT NULL,             -- sale | delivery | hold_created | hold_collected
                                             -- | hold_expired | tool_call | guardrail_violation
                                             -- | call_started | call_ended
  channel         TEXT,                      -- browser | phone | pos | staff
  call_id         TEXT,
  trace_id        TEXT,                      -- LangSmith trace id (Phase 5)
  detail          TEXT NOT NULL              -- JSON
);

CREATE INDEX IF NOT EXISTS idx_holds_active   ON holds(location_id, item_id, status);
CREATE INDEX IF NOT EXISTS idx_alerts_open    ON staff_alerts(acknowledged, at);
CREATE INDEX IF NOT EXISTS idx_events_kind_at ON events(kind, at);
CREATE INDEX IF NOT EXISTS idx_events_call    ON events(call_id);
