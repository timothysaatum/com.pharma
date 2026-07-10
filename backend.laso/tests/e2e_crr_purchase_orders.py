"""
CRR Sync — purchase_orders End-to-End (keep_both_renumber)
===========================================================
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from datetime import date, datetime
from typing import Any, Dict, Optional

import asyncpg

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("e2e_crr_po")

TEST_PREFIX = "e2e-crr-po-"
PG_BRANCH_ID: str = ""
PG_SUPPLIER_ID: str = ""
PG_USER_ID: str = ""

PG_COLUMNS = [
    "id", "organization_id", "branch_id", "po_number", "supplier_id",
    "subtotal", "tax_amount", "shipping_cost", "total_amount",
    "status", "ordered_by", "approved_by", "approved_at",
    "expected_delivery_date", "received_date", "notes", "items_json",
    "created_at", "updated_at", "sync_version", "sync_status",
    "last_synced_at", "sync_hash",
]


async def pg_connect() -> asyncpg.Connection:
    url = os.environ.get("DATABASE_URL", "postgresql://vermithor1:laso_dev_2024@localhost:5432/laso_db")
    return await asyncpg.connect(url.replace("+asyncpg", ""))


def _pgify_val(key: str, val: Any) -> Any:
    if val is None:
        return None
    if key in ("updated_at", "created_at", "last_synced_at", "approved_at"):
        if isinstance(val, str) and val.strip():
            try:
                return datetime.fromisoformat(val.replace("Z", "+00:00"))
            except ValueError:
                pass
        return None
    if key in ("expected_delivery_date", "received_date"):
        if isinstance(val, str) and val.strip():
            try:
                return datetime.fromisoformat(val.replace("Z", "+00:00")).date()
            except ValueError:
                pass
        return None
    if key in ("subtotal", "tax_amount", "shipping_cost", "total_amount"):
        try:
            return float(val)
        except (TypeError, ValueError):
            return None
    if key in ("sync_version",):
        try:
            return int(val)
        except (TypeError, ValueError):
            return 0
    return val


async def _disambiguate_bk(bk: str, pg_conn) -> str:
    for suffix in "BCDEFGHIJKLMNOPQRSTUVWXYZ":
        candidate = f"{bk}-{suffix}"
        exists = await pg_conn.fetchval(
            "SELECT 1 FROM purchase_orders WHERE po_number = $1 LIMIT 1", candidate,
        )
        if not exists:
            return candidate
    raise RuntimeError(f"Cannot disambiguate {bk}")


async def _pg_upsert(pg, row) -> Optional[Dict]:
    vals = {}
    for col in PG_COLUMNS:
        raw = row.get(col)
        if col == "sync_status" and raw is None:
            raw = "synced"
        vals[col] = _pgify_val(col, raw)

    branch_id = vals.get("branch_id")
    po_num = vals.get("po_number")
    new_id = vals.get("id")
    if branch_id and po_num and new_id:
        existing = await pg.fetchrow(
            "SELECT id, po_number FROM purchase_orders "
            "WHERE branch_id = $1 AND po_number = $2 AND id != $3",
            branch_id, po_num, new_id,
        )
        if existing is not None:
            existing_id, old_bk = existing["id"], existing["po_number"]
            new_bk = await _disambiguate_bk(po_num, pg)
            vals["po_number"] = new_bk
            cols = [c for c in PG_COLUMNS if vals.get(c) is not None]
            col_list = ", ".join(cols)
            ph = ", ".join(f"${i+1}" for i in range(len(cols)))
            updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "id")
            values = [vals[c] for c in cols]
            await pg.execute(
                f"INSERT INTO purchase_orders ({col_list}) VALUES ({ph}) ON CONFLICT (id) DO UPDATE SET {updates}", values,
            )
            await pg.execute(
                "INSERT INTO crr_renumber_audit (table_name, winner_id, loser_id, business_key_col, old_business_key, new_business_key) VALUES ($1,$2,$3,$4,$5,$6)",
                "purchase_orders", existing_id, new_id, "po_number", old_bk, new_bk,
            )
            logger.info("  🔄 PO renumbered: %s -> %s", old_bk, new_bk)
            return {"winner_id": existing_id, "loser_id": new_id, "old_bk": old_bk, "new_bk": new_bk}

    cols = [c for c in PG_COLUMNS if vals.get(c) is not None]
    if not cols:
        return None
    col_list = ", ".join(cols)
    ph = ", ".join(f"${i+1}" for i in range(len(cols)))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "id")
    values = [vals[c] for c in cols]
    await pg.execute(
        f"INSERT INTO purchase_orders ({col_list}) VALUES ({ph}) ON CONFLICT (id) DO UPDATE SET {updates}", values,
    )
    return None


async def _cleanup(pg):
    await pg.execute("DELETE FROM purchase_orders WHERE id LIKE $1", f"{TEST_PREFIX}%")
    await pg.execute("DELETE FROM crr_renumber_audit WHERE winner_id LIKE $1", f"{TEST_PREFIX}%")


async def test_upsert(pg):
    logger.info("=" * 60)
    logger.info("SCENARIO 1: Postgres upsert")
    logger.info("=" * 60)
    row_id = f"{TEST_PREFIX}u-{uuid.uuid4().hex[:8]}"
    now = "2026-07-10T12:00:00Z"
    row = {
        "id": row_id, "organization_id": (await pg.fetchval("SELECT organization_id FROM branches WHERE id=$1", PG_BRANCH_ID)),
        "branch_id": PG_BRANCH_ID, "po_number": "PO-NORMAL", "supplier_id": PG_SUPPLIER_ID,
        "subtotal": 100.00, "tax_amount": 10.00, "shipping_cost": 5.00, "total_amount": 115.00,
        "status": "draft", "ordered_by": PG_USER_ID, "items_json": "[]",
        "sync_version": 1, "sync_status": "synced", "updated_at": now, "created_at": now,
    }
    await _pg_upsert(pg, row)
    r = await pg.fetchrow("SELECT po_number, subtotal FROM purchase_orders WHERE id=$1", row_id)
    assert r["po_number"] == "PO-NORMAL"
    assert float(r["subtotal"]) == 100.00
    logger.info("  ✅ Scenario 1 PASSED\n")


async def test_keep_both_renumber(pg):
    logger.info("=" * 60)
    logger.info("SCENARIO 2: keep_both_renumber")
    logger.info("=" * 60)
    id_a = f"{TEST_PREFIX}da-{uuid.uuid4().hex[:8]}"
    id_b = f"{TEST_PREFIX}db-{uuid.uuid4().hex[:8]}"
    org_id = await pg.fetchval("SELECT organization_id FROM branches WHERE id=$1", PG_BRANCH_ID)
    now = "2026-07-10T12:00:00Z"

    row_a = {
        "id": id_a, "organization_id": org_id, "branch_id": PG_BRANCH_ID,
        "po_number": "PO-COL", "supplier_id": PG_SUPPLIER_ID,
        "subtotal": 50.00, "tax_amount": 5.00, "shipping_cost": 0, "total_amount": 55.00,
        "status": "draft", "ordered_by": PG_USER_ID, "items_json": "[]",
        "sync_version": 1, "sync_status": "synced", "updated_at": now, "created_at": now,
    }
    await _pg_upsert(pg, row_a)
    assert await pg.fetchval("SELECT po_number FROM purchase_orders WHERE id=$1", id_a) == "PO-COL"
    logger.info("  ✅ PO A: PO-COL")

    row_b = {
        "id": id_b, "organization_id": org_id, "branch_id": PG_BRANCH_ID,
        "po_number": "PO-COL", "supplier_id": PG_SUPPLIER_ID,
        "subtotal": 200.00, "tax_amount": 20.00, "shipping_cost": 10.00, "total_amount": 230.00,
        "status": "approved", "ordered_by": PG_USER_ID, "items_json": "[{\"drug\":\"X\"}]",
        "sync_version": 1, "sync_status": "synced", "updated_at": now, "created_at": now,
    }
    result = await _pg_upsert(pg, row_b)
    assert result is not None
    assert result["new_bk"] == "PO-COL-B"

    count = await pg.fetchval("SELECT COUNT(*) FROM purchase_orders WHERE branch_id=$1 AND po_number IN ('PO-COL','PO-COL-B')", PG_BRANCH_ID)
    assert count == 2
    a2 = await pg.fetchrow("SELECT po_number, subtotal FROM purchase_orders WHERE id=$1", id_a)
    assert a2["po_number"] == "PO-COL"
    assert float(a2["subtotal"]) == 50.00
    b2 = await pg.fetchrow("SELECT po_number, subtotal, status, items_json FROM purchase_orders WHERE id=$1", id_b)
    assert b2["po_number"] == "PO-COL-B"
    assert float(b2["subtotal"]) == 200.00
    assert b2["status"] == "approved"
    logger.info("  ✅ Both rows: PO-COL + PO-COL-B, data intact")

    audit = await pg.fetchrow("SELECT * FROM crr_renumber_audit WHERE loser_id=$1", id_b)
    assert audit is not None
    assert audit["old_business_key"] == "PO-COL"
    assert audit["new_business_key"] == "PO-COL-B"
    assert audit["table_name"] == "purchase_orders"
    logger.info("  ✅ Audit log verified")

    logger.info("  ✅ Scenario 2 PASSED\n")


async def test_type_coercion(pg):
    logger.info("=" * 60)
    logger.info("SCENARIO 3: Type coercion")
    logger.info("=" * 60)
    row_id = f"{TEST_PREFIX}t-{uuid.uuid4().hex[:8]}"
    org_id = await pg.fetchval("SELECT organization_id FROM branches WHERE id=$1", PG_BRANCH_ID)
    now = "2026-07-10T12:00:00Z"
    row = {
        "id": row_id, "organization_id": org_id, "branch_id": PG_BRANCH_ID,
        "po_number": "PO-TYPE", "supplier_id": PG_SUPPLIER_ID,
        "subtotal": 99.99, "tax_amount": 0, "shipping_cost": 0, "total_amount": 99.99,
        "status": "received", "ordered_by": PG_USER_ID, "items_json": "[]",
        "sync_version": 1, "sync_status": "synced", "updated_at": now, "created_at": now,
    }
    await _pg_upsert(pg, row)
    r = await pg.fetchrow("SELECT subtotal, total_amount, status FROM purchase_orders WHERE id=$1", row_id)
    assert float(r["subtotal"]) == 99.99
    assert float(r["total_amount"]) == 99.99
    assert r["status"] == "received"
    logger.info("  ✅ Money → NUMERIC, status → VARCHAR")
    logger.info("  ✅ Scenario 3 PASSED\n")


async def main():
    pg = await pg_connect()
    logger.info("Postgres OK")
    global PG_BRANCH_ID, PG_SUPPLIER_ID, PG_USER_ID
    PG_BRANCH_ID = await pg.fetchval("SELECT id FROM branches ORDER BY created_at LIMIT 1")
    PG_SUPPLIER_ID = await pg.fetchval("SELECT id FROM suppliers ORDER BY created_at LIMIT 1")
    PG_USER_ID = await pg.fetchval("SELECT id FROM users ORDER BY created_at LIMIT 1")
    logger.info("branch=%s supplier=%s user=%s", PG_BRANCH_ID[:8] if PG_BRANCH_ID else "?", PG_SUPPLIER_ID[:8] if PG_SUPPLIER_ID else "?", PG_USER_ID[:8] if PG_USER_ID else "?")
    try:
        await _cleanup(pg)
        await test_upsert(pg)
        await _cleanup(pg)
        await test_keep_both_renumber(pg)
        await _cleanup(pg)
        await test_type_coercion(pg)
        await _cleanup(pg)
        logger.info("  ALL purchase_orders SCENARIOS PASSED ✅")
    finally:
        await _cleanup(pg)
        await pg.close()

if __name__ == "__main__":
    asyncio.run(main())
