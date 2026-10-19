# ADR 0004: Holds are idempotent on (call, item, quantity, pickup minute)

**Status:** accepted. **Date:** 2026-10-18.

## Context

Models sometimes call `create_hold` twice in one turn. Without protection that is two cakes
promised for one request, and the second hold silently eats another caller's stock.

## Decision

A key derived from `(call_id, location_id, item_id, quantity, pickup minute)` is looked up inside
the same `BEGIN IMMEDIATE` transaction as the insert, and a partial unique index applies while a hold
is live. Pickup truncates to the minute because two resolutions of "in twenty minutes" within one
turn land microseconds apart, so a key built from the raw instant would dedupe nothing.

## Consequences

A replayed call returns the existing hold with a `note`. A customer can legitimately order the same
thing again after collecting, because the index ignores resolved holds. Staff-created holds carry no
call id and are never deduplicated. The eval harness counts double calls as a model-behaviour
statistic.
