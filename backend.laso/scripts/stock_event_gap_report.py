"""
Gap analysis: what does the real org's event log actually cover?

Read-only. Runs SELECTs only and never opens a writing transaction, so it is safe
against the production database.

Reports, per drug: which of drugs / categories / branch_inventory / drug_batches
have a corresponding event in the log, and which have none. A device can only
reconstruct local state from events, so an entity with no event is invisible to
every device regardless of what the tables contain.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any, Dict, List

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# Real identifiers from the investigation, so the output is recognisable.
ORG_ID = "2d060ef8-a302-447c-91f4-b2fd30268341"
BRANCH_ID = "72b2433d-120b-42a2-918b-e6dfcf176b1a"
NIL_UUID = "00000000-0000-0000-0000-000000000000"


def _url() -> str:
    url = (
        os.environ.get("BACKFILL_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or ""
    )
    if not url:
        raise SystemExit(
            "Set BACKFILL_DATABASE_URL (or DATABASE_URL) to the database to "
            "analyse. This script has no default and will not guess."
        )
    return url


async def collect(url: str, org_id: str, branch_id: str) -> Dict[str, Any]:
    engine = create_async_engine(url, poolclass=None)

    # A read-only transaction: even if a future edit added a write, the database
    # would reject it rather than mutating production.
    async with engine.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))

        drugs = (
            await conn.execute(
                text(
                    """
                    SELECT id, name, sku, is_active, is_deleted, reorder_level,
                           reorder_quantity, unit_price, cost_price, category_id
                      FROM drugs
                     WHERE organization_id = :org
                       AND is_deleted = false
                     ORDER BY name
                    """
                ),
                {"org": org_id},
            )
        ).mappings().all()

        inventory = (
            await conn.execute(
                text(
                    """
                    SELECT id, drug_id, branch_id, quantity, reserved_quantity,
                           selling_price, location, sync_status
                      FROM branch_inventory
                     WHERE branch_id = :branch
                    """
                ),
                {"branch": branch_id},
            )
        ).mappings().all()

        batches = (
            await conn.execute(
                text(
                    """
                    SELECT id, drug_id, branch_id, batch_number, quantity,
                           remaining_quantity, expiry_date, cost_price,
                           selling_price, supplier, purchase_order_id, sync_status
                      FROM drug_batches
                     WHERE branch_id = :branch
                    """
                ),
                {"branch": branch_id},
            )
        ).mappings().all()

        categories = (
            await conn.execute(
                text(
                    """
                    SELECT id, name, parent_id, level
                      FROM drug_categories
                     WHERE organization_id = :org
                       AND is_deleted = false
                     ORDER BY name
                    """
                ),
                {"org": org_id},
            )
        ).mappings().all()

        contracts = (
            await conn.execute(
                text(
                    """
                    SELECT id, name, contract_type, status, is_default
                      FROM price_contracts
                     WHERE organization_id = :org
                       AND is_deleted = false
                    """
                ),
                {"org": org_id},
            )
        ).mappings().all()

        # Which aggregate ids already have events?
        evented_aggregates = {
            r["aggregate_id"]
            for r in (
                await conn.execute(
                    text(
                        """
                        SELECT DISTINCT aggregate_id FROM event_log WHERE org_id = :org
                        """
                    ),
                    {"org": org_id},
                )
            ).mappings().all()
        }

        event_types = (
            await conn.execute(
                text(
                    """
                    SELECT event_type, count(*) AS n, min(seq) AS lo, max(seq) AS hi
                      FROM event_log
                     WHERE org_id = :org
                     GROUP BY event_type
                     ORDER BY event_type
                    """
                ),
                {"org": org_id},
            )
        ).mappings().all()

        head = (
            await conn.execute(
                text("SELECT COALESCE(MAX(seq),0) FROM event_log WHERE org_id = :org"),
                {"org": org_id},
            )
        ).scalar_one()

        batches_by_drug: Dict[str, List[Any]] = {}
        for b in batches:
            batches_by_drug.setdefault(str(b["drug_id"]), []).append(b)
        inv_by_drug = {str(r["drug_id"]): r for r in inventory}

        rows = []
        for d in drugs:
            did = str(d["id"])
            inv = inv_by_drug.get(did)
            dbatches = batches_by_drug.get(did, [])
            rows.append(
                {
                    "drug_id": did,
                    "name": d["name"],
                    "sku": d["sku"],
                    "category_id": str(d["category_id"]) if d["category_id"] else None,
                    "inventory": inv,
                    "batches": dbatches,
                    "has_drug_event": did in evented_aggregates,
                    "has_inventory_event": bool(inv) and str(inv["id"]) in evented_aggregates,
                    "has_any_batch_event": any(str(b["id"]) in evented_aggregates for b in dbatches),
                }
            )

    await engine.dispose()
    return {
        "rows": rows,
        "categories": categories,
        "contracts": contracts,
        "event_types": event_types,
        "head": head,
        "n_inventory": len(inventory),
        "n_batches": len(batches),
    }


def render(data: Dict[str, Any], org_id: str, branch_id: str) -> None:
    print(f"organization : {org_id}")
    print(f"branch       : {branch_id}")
    print(f"event_log head (max seq) : {data['head']}")
    print(f"branch_inventory rows    : {data['n_inventory']}")
    print(f"drug_batches rows        : {data['n_batches']}")
    print()

    print("event_log composition by type")
    for r in data["event_types"]:
        print(f"  {r['event_type']:<34} {r['n']:>5}  seq {r['lo']}..{r['hi']}")
    print()

    print("PER-ENTITY EVENT COVERAGE")
    header = (
        f"{'drug':<20}{'drug_id':<38}{'bi_qty':>7}"
        f"{'batches':>8}{'drug_evt':>10}{'bi_evt':>8}{'batch_evt':>11}"
    )
    print(header)
    print("-" * len(header))
    gap_total = 0
    for r in data["rows"]:
        inv_qty = r["inventory"]["quantity"] if r["inventory"] else None
        missing = []
        if not r["has_drug_event"]:
            missing.append("drug_created")
        if r["inventory"] and not r["has_inventory_event"]:
            missing.append("branch_inventory_created")
        if r["batches"] and not r["has_any_batch_event"]:
            missing.append("drug_batch_created")
        if missing:
            gap_total += 1
        print(
            f"{r['name'][:19]:<20}{r['drug_id']:<38}"
            f"{('-' if inv_qty is None else inv_qty):>7}"
            f"{len(r['batches']):>8}"
            f"{('yes' if r['has_drug_event'] else 'NO'):>10}"
            f"{('yes' if r['has_inventory_event'] else ('NO' if r['inventory'] else '-')):>8}"
            f"{('yes' if r['has_any_batch_event'] else ('NO' if r['batches'] else '-')):>11}"
            + (f"   MISSING: {', '.join(missing)}" if missing else "")
        )
    print()
    print(f"drugs with at least one missing event type: {gap_total} of {len(data['rows'])}")
    print()

    print("CATEGORIES (report only, not backfilled)")
    if data["categories"]:
        for c in data["categories"]:
            print(f"  {c['name']:<30} {c['id']}  level={c['level']}")
    else:
        print("  none")
    print()

    print("PRICE CONTRACTS (report only, not backfilled)")
    if data["contracts"]:
        for c in data["contracts"]:
            print(f"  {c['name']:<30} {c['id']}  type={c['contract_type']} status={c['status']} default={c['is_default']}")
    else:
        print("  none")
    print()

    print("BATCH DETAIL (for the dry-run table)")
    for r in data["rows"]:
        if not r["batches"]:
            continue
        print(f"  {r['name']}")
        for b in r["batches"]:
            print(
                f"    batch {b['batch_number']:<12} id={b['id']}"
                f"  qty={b['quantity']} remaining={b['remaining_quantity']}"
                f"  expiry={b['expiry_date']}"
            )


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--org-id", default=ORG_ID)
    ap.add_argument("--branch-id", default=BRANCH_ID)
    args = ap.parse_args()

    data = await collect(_url(), args.org_id, args.branch_id)
    render(data, args.org_id, args.branch_id)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))