#!/usr/bin/env python3
"""
backfill_stock_events.py — author the events PostgreSQL never received.

THE PROBLEM
-----------
Every branch_inventory and drug_batches row in the event log was written
directly by SQL or a seed, not by a service call, so no event was ever
published for them. A device can only learn stock from events, which is why the
six real drugs have stock in PostgreSQL and nothing on any device.

WHAT IT DOES
------------
Reads the current truth from the server and, for everything missing from the
event log, appends the corresponding events through the SAME
StockEventEmitter / AppendService path the application uses — one transaction,
batch detail included, so a failed append leaves nothing behind.

Emits, in this order for readability (order is not load-bearing any more: the
device derives branch_inventory.quantity from its lots, so delivery order cannot
change the result):
    drug_category_created -> drug_created -> branch_inventory_created
    -> drug_batch_created (one per lot)

SAFETY
------
* Dry-run by DEFAULT. Nothing is appended, and the connection is opened
  read-only so the database itself refuses a write.
* --apply additionally requires --i-have-a-backup. This script has never been
  run with --apply by me and must not be: the owner takes a pg_dump first and
  runs it themselves.
* event_ids are DETERMINISTIC, derived from (event_type, aggregate_id, version
  tag), so a re-run appends nothing — the append path returns ALREADY_APPENDED
  for a repeated id. Real row ids are reused as aggregate_ids.
* Price contracts are REPORTED as a gap and deliberately NOT backfilled: a
  contract event would change how every device prices sales, which is a pricing
  decision, not a stock repair.

Usage:
    python backfill_stock_events.py                      # dry-run (default)
    python backfill_stock_events.py --apply --i-have-a-backup
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

ENV_PATH = BACKEND / ".env"

# The org and branch this backfill is scoped to. Both are required: a backfill
# that guessed its scope would publish another org's stock. Overridable only so
# the test suite can exercise it against a disposable cluster.
DEFAULT_ORG_ID = uuid.UUID("2d060ef8-a302-447c-91f4-b2fd30268341")
DEFAULT_BRANCH_ID = uuid.UUID("72b2433d-120b-42a2-918b-e6dfcf176b1a")
DEFAULT_AUTHOR_ID = uuid.UUID("bae475d9-994a-4d5b-abb2-32aa4b082602")

# Set by main() from the CLI. Module-level so the planning helpers can reach them
# without threading two uuids through every function.
ORG_ID = DEFAULT_ORG_ID
BRANCH_ID = DEFAULT_BRANCH_ID
AUTHOR_ID = DEFAULT_AUTHOR_ID

# Bumped when the payload shape changes, so a re-run after a shape change
# appends NEW events rather than colliding with the old ones.
VERSION_TAG = "backfill-v1"


# ── Connection ────────────────────────────────────────────────────────────────


def _db_url() -> str:
    """Target database.

    The environment wins over .env so the test suite can aim this at a
    disposable cluster. That ordering matters for safety: without it a test
    could silently run --apply against atlasdb because .env said so.
    """
    env = os.environ.get("DATABASE_URL") or os.environ.get("TEST_DATABASE_URL")
    if env:
        return env
    for line in ENV_PATH.read_text().splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no DATABASE_URL in the environment or .env")


def connect(read_only: bool):
    """Open a connection. read_only=True sets a READ ONLY transaction.

    On a dry run this is the guarantee, not a convention: PostgreSQL rejects any
    write attempted inside such a transaction.
    """
    url = _db_url()
    # Tolerate an optional password: a trust-auth cluster has none
    # (postgresql://postgres@localhost:5432/db), and a query string may follow
    # the database name (the disposable test cluster uses ?host=...&port=...).
    m = re.match(
        r"postgresql(?:\+\w+)?://"
        r"(?:(?P<user>[^:@/]+)(?::(?P<pw>[^@]*))?@)?"
        r"(?P<host>[^:/@]+)(?::(?P<port>\d+))?"
        r"/(?P<db>[^?]+)",
        url,
    )
    if not m:
        raise SystemExit(f"could not parse DATABASE_URL: {url.split('@')[-1]}")
    user = m.group("user") or "postgres"
    password = m.group("pw") or ""
    host = m.group("host")
    port = int(m.group("port") or 5432)
    dbname = m.group("db")

    # Honour a query string. SQLAlchemy reads ?host=/path/to/socket&port=N to
    # switch a unix-socket cluster, and the disposable test cluster listens on a
    # socket only, so without this psycopg2 dials TCP and is refused.
    query = url.split("?", 1)[1] if "?" in url else ""
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
        host=host, port=int(port), dbname=dbname, user=user, password=password
    )
    if read_only:
        conn.set_session(readonly=True, autocommit=False)
    else:
        conn.set_session(readonly=False, autocommit=False)
    return conn


# ── Planning ──────────────────────────────────────────────────────────────────


@dataclass
class PlannedEvent:
    event_type: str
    aggregate_id: uuid.UUID
    label: str
    payload: dict[str, Any]


@dataclass
class Plan:
    categories: list[PlannedEvent] = field(default_factory=list)
    drugs: list[PlannedEvent] = field(default_factory=list)
    inventories: list[PlannedEvent] = field(default_factory=list)
    batches: list[PlannedEvent] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)
    contract_gap: list[str] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=dict)

    @property
    def everything(self) -> list[PlannedEvent]:
        return self.categories + self.drugs + self.inventories + self.batches

    def for_drug(self, drug_id: str) -> list[PlannedEvent]:
        out: list[PlannedEvent] = []
        for e in self.categories:
            if str(e.aggregate_id) in self._drug_to_categories.get(drug_id, set()):
                out.append(e)
        out += [e for e in self.drugs if str(e.aggregate_id) == drug_id]
        out += [
            e for e in self.inventories if str(e.payload.get("drug_id")) == drug_id
        ]
        out += [
            e for e in self.batches if str(e.payload.get("drug_id")) == drug_id
        ]
        return out

    _drug_to_categories: dict[str, set[str]] = field(default_factory=dict)


def existing_event_ids(conn) -> set[str]:
    """Event ids already in the log, so the plan can skip them."""
    import psycopg2.extras

    with conn.cursor() as cur:
        cur.execute("SELECT event_id FROM event_log WHERE org_id = %s", (str(ORG_ID),))
        return {r[0] for r in cur.fetchall()}


def existing_aggregates(conn) -> set[tuple[str, str]]:
    """(aggregate_type, aggregate_id) pairs already published.

    Used to decide whether a row is MISSING rather than merely un-backfilled by
    this script: a drug that already has a drug_created event is not a gap.
    """
    import psycopg2.extras

    with conn.cursor() as cur:
        cur.execute(
            "SELECT aggregate_type, aggregate_id::text FROM event_log WHERE org_id = %s",
            (str(ORG_ID),),
        )
        return {(r[0], r[1]) for r in cur.fetchall()}


def build_plan(conn) -> Plan:
    import psycopg2.extras

    plan = Plan()
    have_events = existing_event_ids(conn)
    have_agg = existing_aggregates(conn)

    from app.services.sync.eventlog.stock_emitter import (
        branch_inventory_payload,
        deterministic_event_id,
        drug_batch_payload,
    )

    # ── drug categories ──────────────────────────────────────────────────
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT DISTINCT c.id, c.name, c.parent_id, c.sync_status
              FROM drug_categories c
              JOIN drugs d ON d.category_id = c.id
             WHERE d.organization_id = %s
             ORDER BY c.name
            """,
            (str(ORG_ID),),
        )
        categories = cur.fetchall()

    for c in categories:
        cid = str(c["id"])
        if ("drug_category", cid) in have_agg:
            plan.already_present.append(f"drug_category {c['name']}")
            continue
        eid = deterministic_event_id("drug_category_created", cid, VERSION_TAG)
        if eid in have_events:
            plan.already_present.append(f"drug_category {c['name']} (id present)")
            continue
        plan.categories.append(
            PlannedEvent(
                event_type="drug_category_created",
                aggregate_id=c["id"],
                label=f"drug_category {c['name']}",
                payload={
                    "drug_category_id": cid,
                    "name": c["name"],
                    "parent_id": str(c["parent_id"]) if c["parent_id"] else None,
                    "description": None,
                },
            )
        )

    # ── drugs ────────────────────────────────────────────────────────────
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT d.id, d.name, d.generic_name, d.brand_name, d.sku, d.barcode,
                   d.category_id, d.reorder_quantity, d.reorder_level,
                   d.unit_price, d.cost_price, d.dosage_form, d.strength,
                   d.drug_type, d.manufacturer, d.supplier, d.unit_of_measure,
                   d.tax_rate, d.description, d.requires_prescription
              FROM drugs d
              JOIN branch_inventory bi
                ON bi.drug_id = d.id AND bi.branch_id = %s
             WHERE d.organization_id = %s AND d.is_deleted = false
             ORDER BY d.name
            """,
            (str(BRANCH_ID), str(ORG_ID)),
        )
        drugs = cur.fetchall()

    for d in drugs:
        did = str(d["id"])
        if d["category_id"]:
            plan._drug_to_categories.setdefault(str(d["category_id"]), set()).add(did)
        if ("drug", did) in have_agg:
            plan.already_present.append(f"drug {d['name']}")
            continue
        eid = deterministic_event_id("drug_created", did, VERSION_TAG)
        if eid in have_events:
            plan.already_present.append(f"drug {d['name']} (id present)")
            continue
        plan.drugs.append(
            PlannedEvent(
                event_type="drug_created",
                aggregate_id=d["id"],
                label=f"drug {d['name']}",
                payload={
                    "drug_id": did,
                    "name": d["name"],
                    "generic_name": d["generic_name"],
                    "brand_name": d["brand_name"],
                    "sku": d["sku"],
                    "barcode": d["barcode"],
                    "category_id": str(d["category_id"]) if d["category_id"] else None,
                    "reorder_quantity": int(d["reorder_quantity"] or 0),
                    "reorder_level": int(d["reorder_level"] or 0),
                    "unit_price": str(d["unit_price"]) if d["unit_price"] is not None else None,
                    "cost_price": str(d["cost_price"]) if d["cost_price"] is not None else None,
                    "dosage_form": d["dosage_form"],
                    "strength": d["strength"],
                    "drug_type": d["drug_type"],
                    "manufacturer": d["manufacturer"],
                    "supplier": d["supplier"],
                    "unit_of_measure": d["unit_of_measure"],
                    "tax_rate": str(d["tax_rate"]) if d["tax_rate"] is not None else None,
                    "description": d["description"],
                    "requires_prescription": d["requires_prescription"],
                },
            )
        )

    # ── branch_inventory + drug_batches ──────────────────────────────────
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT bi.id, bi.drug_id, bi.quantity, bi.reserved_quantity,
                   bi.location, bi.selling_price
              FROM branch_inventory bi
             WHERE bi.branch_id = %s
             ORDER BY bi.drug_id
            """,
            (str(BRANCH_ID),),
        )
        inventories = cur.fetchall()

        cur.execute(
            """
            SELECT id, drug_id, batch_number, quantity, remaining_quantity,
                   expiry_date, cost_price, selling_price, supplier,
                   manufacturing_date, purchase_order_id
              FROM drug_batches
             WHERE branch_id = %s
             ORDER BY drug_id, batch_number
            """,
            (str(BRANCH_ID),),
        )
        batches = cur.fetchall()

    inv_by_drug = {str(i["drug_id"]): i for i in inventories}

    for inv in inventories:
        iid = str(inv["id"])
        if ("branch_inventory", iid) in have_agg:
            plan.already_present.append(f"branch_inventory {iid}")
            continue
        eid = deterministic_event_id("branch_inventory_created", iid, VERSION_TAG)
        if eid in have_events:
            plan.already_present.append(f"branch_inventory {iid} (id present)")
            continue
        plan.inventories.append(
            PlannedEvent(
                event_type="branch_inventory_created",
                aggregate_id=inv["id"],
                label=f"branch_inventory drug={inv['drug_id']}",
                payload=branch_inventory_payload(
                    inventory_id=inv["id"],
                    branch_id=BRANCH_ID,
                    drug_id=inv["drug_id"],
                    quantity=int(inv["quantity"]),
                    reserved_quantity=int(inv["reserved_quantity"] or 0),
                    location=inv["location"],
                    selling_price=(
                        float(inv["selling_price"])
                        if inv["selling_price"] is not None
                        else None
                    ),
                ),
            )
        )

    for b in batches:
        bid = str(b["id"])
        if ("drug_batch", bid) in have_agg:
            plan.already_present.append(f"drug_batch {b['batch_number']}")
            continue
        eid = deterministic_event_id("drug_batch_created", bid, VERSION_TAG)
        if eid in have_events:
            plan.already_present.append(f"drug_batch {b['batch_number']} (id present)")
            continue
        plan.batches.append(
            PlannedEvent(
                event_type="drug_batch_created",
                aggregate_id=b["id"],
                label=f"drug_batch {b['batch_number']} drug={b['drug_id']}",
                payload=drug_batch_payload(
                    batch_id=b["id"],
                    drug_id=b["drug_id"],
                    branch_id=BRANCH_ID,
                    batch_number=b["batch_number"],
                    quantity=int(b["quantity"]),
                    # The server's CURRENT truth, not a delta. Under C-hybrid the
                    # device derives quantity from this, so it must be right.
                    remaining_quantity=int(b["remaining_quantity"]),
                    cost_price=(
                        float(b["cost_price"]) if b["cost_price"] is not None else None
                    ),
                    selling_price=(
                        float(b["selling_price"])
                        if b["selling_price"] is not None
                        else None
                    ),
                    expiry_date=(
                        b["expiry_date"].isoformat() if b["expiry_date"] else None
                    ),
                    supplier=b["supplier"],
                    purchase_order_id=(
                        str(b["purchase_order_id"]) if b["purchase_order_id"] else None
                    ),
                ),
            )
        )

    # ── price contracts: REPORTED, never backfilled ──────────────────────
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, contract_code, contract_type, status, is_active
              FROM price_contracts
             WHERE organization_id = %s
             ORDER BY contract_code
            """,
            (str(ORG_ID),),
        )
        for pc in cur.fetchall():
            if ("price_contract", str(pc["id"])) not in have_agg:
                plan.contract_gap.append(
                    f"price_contract {pc['contract_code']} "
                    f"({pc['contract_type']}, status={pc['status']}) "
                    f"id={pc['id']} — NOT BACKFILLED"
                )

    plan.totals = {
        "drug_category": len(plan.categories),
        "drug": len(plan.drugs),
        "branch_inventory": len(plan.inventories),
        "drug_batch": len(plan.batches),
        "contract (reported only)": len(plan.contract_gap),
        "already published": len(plan.already_present),
    }
    return plan


# ── Reporting ─────────────────────────────────────────────────────────────────


def print_gap_table(conn, plan: Plan) -> None:
    import psycopg2.extras

    print()
    print("=" * 100)
    print("GAP TABLE — what exists in PostgreSQL but has no event")
    print(f"org {ORG_ID}  branch {BRANCH_ID}")
    print("=" * 100)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT d.id, d.name, d.sync_status AS drug_sync,
                   c.name AS category, c.sync_status AS cat_sync,
                   bi.quantity, bi.sync_status AS inv_sync
              FROM drugs d
              JOIN branch_inventory bi ON bi.drug_id = d.id AND bi.branch_id = %s
              LEFT JOIN drug_categories c ON c.id = d.category_id
             WHERE d.organization_id = %s AND d.is_deleted = false
             ORDER BY d.name
            """,
            (str(BRANCH_ID), str(ORG_ID)),
        )
        drugs = cur.fetchall()

        cur.execute(
            """
            SELECT drug_id, batch_number, remaining_quantity, expiry_date, sync_status
              FROM drug_batches WHERE branch_id = %s
             ORDER BY drug_id, batch_number
            """,
            (str(BRANCH_ID),),
        )
        lots = cur.fetchall()

    lots_by_drug: dict[str, list] = {}
    for l in lots:
        lots_by_drug.setdefault(str(l["drug_id"]), []).append(l)

    print()
    print("drug | branch_inventory qty | category | drug_created | category event | "
          "inventory event | batches | batch events")
    for d in drugs:
        did = str(d["id"])
        dl = lots_by_drug.get(did, [])
        n_lots = len(dl)
        have_drug = any(str(e.aggregate_id) == did for e in plan.drugs)
        print(
            f"{d['name']} | {d['quantity']} | {d['category'] or '(none)'} | "
            f"{'MISSING' if have_drug else 'PRESENT'} | "
            f"{'PRESENT' if d['category'] and not plan._is_category_gap(d['category']) else ('n/a' if not d['category'] else 'MISSING')} | "
            f"{'PRESENT' if not plan._is_inv_gap(did) else 'MISSING'} | "
            f"{n_lots} | "
            f"{'PRESENT' if n_lots and not plan._is_batch_gap(did) else 'MISSING' if n_lots else 'n/a'}"
        )

    print()
    print("per-batch detail:")
    for l in lots:
        print(
            f"  {l['batch_number']} drug={l['drug_id']} "
            f"remaining={l['remaining_quantity']} expiry={l['expiry_date']} "
            f"sync_status={l['sync_status']} -> "
            f"{'PRESENT' if not plan._is_batch_gap(str(l['drug_id'])) else 'MISSING'}"
        )

    if plan.contract_gap:
        print()
        print("price contracts (REPORTED, deliberately NOT backfilled):")
        for c in plan.contract_gap:
            print(f"  {c}")
    print("=" * 100)


def _decorate(plan: Plan) -> None:
    """Precompute gap lookups so the printers stay readable."""
    plan._is_category_gap = lambda name: any(  # type: ignore[attr-defined]
        e.label == f"drug_category {name}" for e in plan.categories
    )
    plan._is_inv_gap = lambda drug_id: any(  # type: ignore[attr-defined]
        str(e.payload.get("drug_id")) == drug_id for e in plan.inventories
    )
    plan._is_batch_gap = lambda drug_id: any(  # type: ignore[attr-defined]
        str(e.payload.get("drug_id")) == drug_id for e in plan.batches
    )


def print_dry_run_table(conn, plan: Plan, current_seq: int) -> None:
    import psycopg2.extras

    print()
    print("=" * 100)
    print("DRY RUN — events that WOULD be created (nothing has been written)")
    print("=" * 100)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT bi.drug_id, d.name, bi.quantity
              FROM branch_inventory bi JOIN drugs d ON d.id = bi.drug_id
             WHERE bi.branch_id = %s ORDER BY d.name
            """,
            (str(BRANCH_ID),),
        )
        invs = cur.fetchall()
        cur.execute(
            """
            SELECT drug_id, batch_number, remaining_quantity, expiry_date
              FROM drug_batches WHERE branch_id = %s
             ORDER BY drug_id, batch_number
            """,
            (str(BRANCH_ID),),
        )
        lots = cur.fetchall()

    lots_by_drug: dict[str, list] = {}
    for l in lots:
        lots_by_drug.setdefault(str(l["drug_id"]), []).append(l)

    seq = current_seq
    print()
    for inv in invs:
        did = str(inv["drug_id"])
        mine = plan.for_drug(did)
        dl = lots_by_drug.get(did, [])
        batch_txt = "; ".join(
            f"{b['batch_number']} rem={b['remaining_quantity']} exp={b['expiry_date']}"
            for b in dl
        ) or "(none)"
        print(f"drug: {inv['name']} ({did})")
        print(f"  branch: {BRANCH_ID}")
        print(f"  quantity: {inv['quantity']}")
        print(f"  batches: {batch_txt}")
        print(f"  events that would be created: {len(mine)}")
        for e in mine:
            print(f"    - {e.event_type:<28} aggregate_id={e.aggregate_id}")
        first, last = seq + 1, seq + len(mine)
        print(f"  resulting seq range: {first}..{last}" if mine else "  resulting seq range: (none)")
        seq = last
        print()

    print("-" * 100)
    print("totals:")
    for k, v in plan.totals.items():
        print(f"  {k:<28} {v}")
    print(f"  {'TOTAL events':<28} {len(plan.everything)}")
    print(f"  seq range that would be used: {current_seq + 1}..{current_seq + len(plan.everything)}")
    print("-" * 100)


# ── Apply ─────────────────────────────────────────────────────────────────────


async def apply_plan(plan: Plan) -> dict[str, int]:
    """Append every planned event through the real emitter, in ONE transaction.

    Uses the application's own AppendService via StockEventEmitter, so the
    hash chain, org lock, seq allocation and ALREADY_APPENDED de-duplication are
    the production ones. Nothing is swallowed: an append failure aborts the whole
    transaction and no events land.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.schemas.event_envelope import AggregateType
    from app.services.sync.eventlog.stock_emitter import (
        StockEventEmitter,
        deterministic_event_id,
    )

    def _agg(event_type: str) -> AggregateType:
        """drug_batch_created -> AggregateType.DRUG_BATCH, and so on.

        Strips the trailing ACTION, not the leading noun:
        drug_category_created -> drug_category (splitting on the first
        underscore would give "drug", which is a different aggregate).
        """
        for action in ("_created", "_updated", "_deleted"):
            if event_type.endswith(action):
                return AggregateType(event_type[: -len(action)])
        raise SystemExit(f"cannot derive an aggregate type from {event_type!r}")

    engine = create_async_engine(_db_url(), future=True)
    counts: dict[str, int] = {"appended": 0, "already_appended": 0, "skipped": 0}
    try:
        async with engine.begin() as conn:
            from sqlalchemy.ext.asyncio import AsyncSession

            db = AsyncSession(bind=conn)
            for e in plan.everything:
                agg = _agg(e.event_type)
                res = await StockEventEmitter.emit_in_transaction(
                    db,
                    org_id=ORG_ID,
                    event_type=e.event_type,
                    aggregate_type=agg,
                    aggregate_id=e.aggregate_id,
                    payload=e.payload,
                    authored_by=AUTHOR_ID,
                    branch_id=BRANCH_ID,
                    event_id=deterministic_event_id(
                        e.event_type, str(e.aggregate_id), VERSION_TAG
                    ),
                )
                status = getattr(res, "status", None)
                name = getattr(status, "value", status)
                if name == "already_appended":
                    counts["already_appended"] += 1
                elif name == "accepted":
                    counts["appended"] += 1
                else:
                    counts["skipped"] += 1
            await db.commit()
    finally:
        await engine.dispose()
    return counts


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually append (requires --i-have-a-backup)")
    ap.add_argument("--i-have-a-backup", action="store_true", help="I have taken a pg_dump")
    ap.add_argument("--org-id", default=str(DEFAULT_ORG_ID))
    ap.add_argument("--branch-id", default=str(DEFAULT_BRANCH_ID))
    ap.add_argument("--author-id", default=str(DEFAULT_AUTHOR_ID))
    args = ap.parse_args()

    global ORG_ID, BRANCH_ID, AUTHOR_ID
    ORG_ID = uuid.UUID(args.org_id)
    BRANCH_ID = uuid.UUID(args.branch_id)
    AUTHOR_ID = uuid.UUID(args.author_id)

    if args.apply and not args.i_have_a_backup:
        print(
            "REFUSING: --apply requires --i-have-a-backup.\n"
            "         Take a pg_dump of atlasdb first, then re-run with both flags."
        )
        return 2

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"backfill_stock_events.py — {mode}")
    print(f"org {ORG_ID}  branch {BRANCH_ID}  author {AUTHOR_ID}  version {VERSION_TAG}")

    conn = connect(read_only=not args.apply)
    try:
        plan = build_plan(conn)
        _decorate(plan)

        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(seq), 0) FROM event_log WHERE org_id = %s",
                        (str(ORG_ID),))
            current_seq = int(cur.fetchone()[0])
        print(f"current org head seq: {current_seq}")

        print_gap_table(conn, plan)
        print_dry_run_table(conn, plan, current_seq)

        if not args.apply:
            print()
            print("DRY RUN COMPLETE — nothing was written. The connection was READ ONLY,")
            print("so a write would have been rejected by PostgreSQL itself.")
            print("Re-run with --apply --i-have-a-backup to append (owner only).")
            return 0

        print()
        print(f"appending {len(plan.everything)} events in ONE transaction...")
        counts = asyncio_run(apply_plan(plan))
        print(f"  appended:        {counts['appended']}")
        print(f"  already_appended:{counts['already_appended']}")
        print(f"  skipped:         {counts['skipped']}")
        return 0
    finally:
        conn.rollback()
        conn.close()


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


if __name__ == "__main__":
    sys.exit(main())
