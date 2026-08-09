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

# This row's CRR merge was rejected by application-level validation on an
# earlier push, and it has no authoritative Postgres row to restore in its
# place (i.e. it never reached Postgres in the first place — see
# restore_rejected_row in shadow_db.py). For a keep_both_renumber table
# (prescriptions, purchase_orders — which never legitimately delete a row
# outside that one corrective path; confirmed no other delete path exists
# anywhere in the app, since these entities must not be hard-deleted for
# audit-trail reasons), that leaves a permanent tombstone: the client keeps
# resending the exact same unpushed local change (same id/site_id/version —
# it has no way to resend differently) and the server can never resolve it.
# This is not retryable — resending the identical bytes will never succeed.
# The client should stop resending this specific row (so it stops blocking
# every other row in the same push batch forever) and surface it to the
# user as needing manual review, rather than silently dropping it or
# retrying it indefinitely.
PERMANENTLY_REJECTED = "permanently_rejected"
