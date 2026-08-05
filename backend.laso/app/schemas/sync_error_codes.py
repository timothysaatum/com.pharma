"""
Shared sync error codes
========================
Structured error codes for ``PushResult.error_code``, used by the client's
sync engine to classify a push failure as retryable-without-penalty (e.g. a
dependency row — a prescription or customer — hasn't synced yet, so the
failure is expected to clear on its own) versus a genuine failure that
should count against the record's retry budget and eventually dead-letter.

These values are the actual wire contract between backend and frontend —
kept in sync manually with ``ui.laso/src/lib/syncErrorCodes.ts``. Do not
rename or remove a value without updating both sides.

Replaces matching prose substrings out of ``PushResult.error`` (fragile: the
frontend once checked for a string this backend never emitted, and the one
substring that did work — "is not yet synced" — was an untyped, unversioned
contract that any wording change could silently break in either direction).
See docs/reviews/2026-08-04-inventory-sync-sales-independent-review.md,
finding S4.
"""

# A record this push depends on (e.g. the prescription a sale references,
# or the customer a prescription references) hasn't reached the server yet.
# The client should retry without incrementing its attempt counter.
DEPENDENCY_NOT_SYNCED = "dependency_not_synced"
