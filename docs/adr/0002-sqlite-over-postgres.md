# ADR 0002: SQLite with WAL, one schema file, no migration framework

**Status:** accepted. **Date:** 2026-10-18.

## Context

The project needs a write lock for the hold race, an append-only event log, and zero setup for a
reviewer cloning the repository. It serves one bakery location.

## Decision

SQLite in WAL mode, one `schema.sql`, `BEGIN IMMEDIATE` for every check-then-write transaction,
one connection per call so ten threads can race without a pool. No Alembic.

## Consequences

The concurrency test is real: ten threads take turns on SQLite's single writer lock. The dashboard
keeps reading during writes because WAL allows it. Postgres would be the production choice for more
than one location or more than one process; nothing above `db.py` would change except the
transaction call and the placeholder style.
