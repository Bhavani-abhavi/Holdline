# ADR 0005: Staff notifications go through adapters; SMS is a documented stub

**Status:** accepted. **Date:** 2026-10-18.

## Context

Staff must learn about holds, approvals and allergy callbacks. Real SMS costs money and needs a
provider account; the demo needs the alert to appear on the staff page within two seconds.

## Decision

One `Notifier` protocol, fanned out to every configured implementation with each isolated so a
failing SMS gateway cannot block the in-app alert. The in-app notifier takes the caller's connection
so the alert commits atomically with the hold that caused it. `SmsNotifier` raises
`NotImplementedError` and is skipped by the fan-out.

## Consequences

Adding real SMS is one class implementing one method. The review request after a collection is
sent from the `collect_hold` call site, unconditionally, because sentiment-gated solicitation
violates Google's review policies and a prompt is a request while a call site is a guarantee.
