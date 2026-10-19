# ADR 0003: The point-of-sale system is mocked, with a real API's shape

**Status:** accepted. **Date:** 2026-10-18.

## Context

Toast and Square expose inventory as a status, a quantity and a sync timestamp per item. Integrating
either needs a merchant account and a sandbox; neither adds anything to the four competencies this
project demonstrates.

## Decision

`inventory.py` stands in for the POS and mirrors that shape exactly, including the sync timestamp
that drives the staleness gate. Swapping in a real POS is one adapter that fills the `stock` table.

## Consequences

The demo controls (simulate a rush, break the sync) exist because the POS is mocked. The staleness
gate is exercised by `force_stale`, which is the only way to demo it without waiting ten minutes.
