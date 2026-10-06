#!/usr/bin/env python3
"""
backfill_customer_loyalty_events.py — publish the loyalty state devices never received.

THE PROBLEM
-----------
`customers.loyalty_points` was a bare counter mutated in place by
`SalesService.process_sale`, and no event ever carried it. Devices therefore had
no way to learn a balance: Joe's device row sat at 0/bronze while the server said
125/silver, and the server's own row was `sync_status='pending'` with nothing
behind it. Every device has been showing a stale balance since the day the first
loyalty sale was rung up.

`process_sale` now emits `customer_loyalty_changed`, so new sales converge on
their own. This script exists for the balances that predate that: it publishes
each customer's CURRENT absolute state once, so devices converge without waiting
for the customer to shop again.

WHAT IT DOES
------------
For every customer in the org, appends ONE `customer_loyalty_changed` carrying the
absolute balance, through the same `StockEventEmitter.emit_in_transaction` path the
application uses, in a single transaction. The device handler writes only
`loyalty_points` and `loyalty_tier`, so this cannot disturb a customer's name or
contact details.

WHY THE EVENT ID IS NOT DERIVED FROM (SALE, DIRECTION)
------------------------------------------------------
The runtime rule (ADR 0010) derives the id from (customer, sale, direction), which
is what makes a replayed sale free. A backfill has no sale, so it uses its own
version tag in the same slot: `customer_loyalty_changed|<customer_id>|<tag>`. The
seed shape and the 26-character rule are identical, so `deterministic_event_id`
still applies and a re-run appends nothing.

SAFETY
------
* Dry-run by DEFAULT. Nothing is appended and the connection is opened READ ONLY,
  so PostgreSQL itself refuses a write.
* --apply additionally requires --i-have-a-backup. This script must not be run
  with --apply by an agent: the owner takes a pg_dump first and runs it.
* event_ids are DETERMINISTIC, so a re-run is a no-op.
* ONE transaction for the whole batch: either every customer converges or none
  does.
* Kwame Nkrumah (88888888-...) is SKIPPED and reported, not deleted and not
  modified. His row is E2E fixture residue carrying an internally inconsistent
  state (150 points labelled gold, which resolve_loyalty_tier(150) contradicts),
  and the owner has reserved that decision.

Usage:
    python backfill_customer_loyalty_events.py                      # dry-run (default)
    python backfill_customer_loyalty_events.py --apply --i-have-a-backup
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

ENV_PATH = BACKEND / ".env"

DEFAULT_ORG_ID = uuid.UUID("2d060ef8-a302-447c-91f4-b2fd30268341")
DEFAULT_AUTHOR_ID = uuid.UUID("bae475d9-994a-4d5b-abb2-32aa4b082602")

#: E2E fixture residue. The owner has reserved this row; see the module docstring.
SKIP_CUSTOMER_IDS = {
    uuid.UUID("88888888-8888-8888-8888-888888888888"): "owner decision pending",
}

ORG_ID = DEFAULT_ORG_ID
AUTHOR_ID = DEFAULT_AUTHOR_ID

#: Bumped when the payload shape changes, so a re-run after a shape change appends
#: NEW events rather than colliding with the old ones.
VERSION_TAG = "loyalty-backfill-v1"


# ── Connection ────────────────────────────────────────────────────────────────


def _db_url() -> str:
    """Target database. The environment wins over .env.

    That ordering matters for safety: without it a test could silently aim
    --apply at atlasdb because .env said so.
    """
    env = os.environ.get("DATABASE_URL") or os.environ.get("TEST_DATABASE_URL")
    if env:
        return env
    for line in ENV_PATH.read_text().splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no DATABASE_URL in the environment or .env")


def connect(read_only: bool):
    """Open a connection; read_only=True sets a READ ONLY transaction."""
    url = _db_url()
    m = re.match(
        r"postgresql(?:\+\w+)?://"
        r"(?:(?P<user>[^:@/]+)(?::(?P<pw>[^@]*))?@)?"
        r"(?P<host>[^:/@]+)(?::(?P<port>\d+))?"
        r"/(?P<db>[^?]+)",
        url,
    )
    if not m:
        raise SystemExit(f"could not parse DATABASE_URL: {url.split('@')[-1]}")
    query = url.split("?", 1)[1] if "?" in url else ""
    host = m.group("host")
    port = int(m.group("port") or 5432)
    for part in query.split("&"):
        if not part:
            continue
        k, _, v = part.partition("=")
        if k == "host" and v:
            host = v
        elif k == "port" and v:
            port = int(v)

    import psycopg2

    conn = psycopg2.connect(
        host=host,
        port=int(port),
        dbname=m.group("db"),
        user=m.group("user") or "postgres",
        password=m.group("pw") or "",
    )
    conn.set_session(readonly=read_only, autocommit=False)
    return conn


# ── Plan ──────────────────────────────────────────────────────────────────────


@dataclass
class PlanItem:
    customer_id: str
    first_name: str
    loyalty_points: int
    loyalty_tier: str
    total_orders: int
    event_id: str
    already_present: bool
    skip_reason: str = ""


@dataclass
class Plan:
    items: list[PlanItem]
    skipped: list[PlanItem]

    @property
    def to_emit(self) -> list[PlanItem]:
        return [i for i in self.items if not i.already_present and not i.skip_reason]


def backfill_event_id(customer_id: Any) -> str:
    """Deterministic id for the one-off convergence event.

    Same seed shape and the same 26-character rule as the runtime
    (customer, sale, direction) derivation in
    `app/services/sync/eventlog/loyalty_event_id.py`; only the version tag
    differs, because a backfill has no sale to name.
    """
    from app.services.sync.eventlog.loyalty_event_id import EVENT_TYPE
    from app.services.sync.eventlog.stock_emitter import deterministic_event_id

    return deterministic_event_id(EVENT_TYPE, str(customer_id), VERSION_TAG)


def build_plan(conn) -> Plan:
    """Read the current truth and decide what is missing."""
    from app.services.sales.utils.sale_helpers import resolve_loyalty_tier

    items: list[PlanItem] = []
    skipped: list[PlanItem] = []

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, first_name, loyalty_points, loyalty_tier, total_orders,
                   is_deleted
              FROM customers
             WHERE organization_id = %s
             ORDER BY first_name NULLS LAST, id
            """,
            (str(ORG_ID),),
        )
        rows = cur.fetchall()

        for cid, first_name, points, tier, orders, is_deleted in rows:
            eid = backfill_event_id(cid)
            points = int(points or 0)
            tier = tier or "bronze"

            skip = SKIP_CUSTOMER_IDS.get(uuid.UUID(str(cid)))
            if skip:
                skipped.append(
                    PlanItem(str(cid), first_name or "", points, tier, int(orders or 0),
                             eid, False, f"skipped - {skip}")
                )
                continue

            if is_deleted:
                skipped.append(
                    PlanItem(str(cid), first_name or "", points, tier, int(orders or 0),
                             eid, False, "skipped - soft deleted")
                )
                continue

            # The invariant the runtime now enforces. A row that violates it is
            # reported rather than published: writing the bad tier would push the
            # inconsistency onto every device.
            expected = resolve_loyalty_tier(points)
            if tier != expected:
                skipped.append(
                    PlanItem(str(cid), first_name or "", points, tier, int(orders or 0),
                             eid, False,
                             f"skipped - tier {tier!r} contradicts points {points} "
                             f"(resolve_loyalty_tier -> {expected!r})")
                )
                continue

            cur.execute(
                "SELECT 1 FROM event_log WHERE org_id = %s AND event_id = %s LIMIT 1",
                (str(ORG_ID), eid),
            )
            already = cur.fetchone() is not None
            items.append(
                PlanItem(str(cid), first_name or "", points, tier,
                         int(orders or 0), eid, already)
            )

    return Plan(items=items, skipped=skipped)


def print_dry_run_table(conn, plan: Plan, current_seq: int) -> None:
    print()
    print(f"org head seq before: {current_seq}")
    print()
    header = f"{'customer':<26} {'name':<14} {'points':>7} {'tier':<9} {'orders':>6}  {'action'}"
    print(header)
    print("-" * len(header))
    for i in plan.items:
        if i.skip_reason:
            action = i.skip_reason
        elif i.already_present:
            action = "already present (no-op on re-run)"
        else:
            action = f"emit {i.event_id}"
        print(f"{i.customer_id:<26} {i.first_name[:13]:<14} {i.loyalty_points:>7} "
              f"{i.loyalty_tier:<9} {i.total_orders:>6}  {action}")
    print()
    print(f"to emit: {len(plan.to_emit)}   already present: "
          f"{sum(1 for i in plan.items if i.already_present)}   "
          f"skipped: {len(plan.skipped)}")


# ── Apply ─────────────────────────────────────────────────────────────────────


async def apply_plan(plan: Plan) -> dict[str, int]:
    """Append every planned event in ONE transaction, through the app's own path."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.models.user.user_model import User
    from app.schemas.event_envelope import AggregateType
    from app.services.sync.eventlog.stock_emitter import StockEventEmitter

    url = _db_url().replace("postgresql+psycopg2://", "postgresql+asyncpg://")
    engine = create_async_engine(url)
    counts = {"appended": 0, "already": 0, "failed": 0}
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            author = await db.get(User, AUTHOR_ID)
            if author is None:
                raise SystemExit(
                    f"author {AUTHOR_ID} does not exist; pass --author-id"
                )
            for item in plan.to_emit:
                res = await StockEventEmitter.emit_in_transaction(
                    db,
                    org_id=ORG_ID,
                    event_type="customer_loyalty_changed",
                    aggregate_type=AggregateType.CUSTOMER,
                    aggregate_id=uuid.UUID(item.customer_id),
                    event_id=item.event_id,
                    payload={
                        "customer_id": item.customer_id,
                        "organization_id": str(ORG_ID),
                        "loyalty_points": item.loyalty_points,
                        "loyalty_tier": item.loyalty_tier,
                        "sale_id": f"backfill:{VERSION_TAG}",
                        "direction": "earn",
                        "source": "backfill",
                    },
                    authored_by=AUTHOR_ID,
                )
                name = getattr(res, "status", None) or getattr(res, "result", "")
                if "ALREADY" in str(name).upper():
                    counts["already"] += 1
                else:
                    counts["appended"] += 1
            await db.commit()
    finally:
        await engine.dispose()
    return counts


def asyncio_run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def main() -> int:
    global ORG_ID, AUTHOR_ID

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually append (requires --i-have-a-backup)")
    ap.add_argument("--i-have-a-backup", action="store_true",
                    help="I have taken a pg_dump")
    ap.add_argument("--org-id", default=str(DEFAULT_ORG_ID))
    ap.add_argument("--author-id", default=str(DEFAULT_AUTHOR_ID))
    args = ap.parse_args()

    ORG_ID = uuid.UUID(args.org_id)
    AUTHOR_ID = uuid.UUID(args.author_id)

    mode = "APPLY" if args.apply else "DRY-RUN (default)"
    if args.apply and not args.i_have_a_backup:
        print("REFUSING: --apply requires --i-have-a-backup.\n"
              "Take a pg_dump first. Nothing was written.", file=sys.stderr)
        return 2

    print(f"backfill_customer_loyalty_events.py — {mode}")
    print(f"org {ORG_ID}  author {AUTHOR_ID}  version {VERSION_TAG}")

    conn = connect(read_only=not args.apply)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(seq), 0) FROM event_log WHERE org_id = %s",
                        (str(ORG_ID),))
            current_seq = cur.fetchone()[0]

        plan = build_plan(conn)
        print_dry_run_table(conn, plan, current_seq)

        for s in plan.skipped:
            if s.skip_reason.startswith("skipped - owner"):
                print(f"  {s.customer_id}: {s.skip_reason}")

        if not args.apply:
            print("\nDry run. Nothing written. Re-run with "
                  "--apply --i-have-a-backup to append.")
            return 0

        counts = asyncio_run(apply_plan(plan))
        print(f"\nappended {counts['appended']}  already present "
              f"{counts['already']}  failed {counts['failed']}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
