"""Real-Postgres smoke test for production ShadowDB merge dispatch.

Exercises every non-customer strategy through ``ShadowDB.upsert_merged_row``.
Customer multi-way/FK behavior remains covered by ``e2e_crr_customers.py``.
All rows use random IDs/business keys and are removed in ``finally``.
"""

import asyncio
import json
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text

from app.db.session import AsyncSessionLocal
from app.services.sync.shadow_db import ShadowDB, _resolve_extension_path


TABLES = {
    "branch_inventory": ("branch_id", "drug_id"),
    "drug_batches": ("branch_id", "drug_id", "batch_number"),
    "prescriptions": ("organization_id", "prescription_number"),
    "purchase_orders": ("branch_id", "po_number"),
    "sales": ("branch_id", "sale_number"),
}


def _shadow_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return value


async def _shadow_row(shadow, db, table, row_id, key_updates, field_updates=None):
    result = (await db.execute(text(f"SELECT * FROM {table} LIMIT 1"))).mappings().first()
    if result is None:
        raise RuntimeError(f"{table} requires one real fixture row")
    columns = {
        item[1] for item in shadow._conn.execute(f"PRAGMA table_info([{table}])").fetchall()
    }
    row = {key: _shadow_value(value) for key, value in dict(result).items() if key in columns}
    row.update({"id": row_id, **key_updates, **(field_updates or {})})
    now = datetime.now(timezone.utc).isoformat()
    row["created_at"] = now
    row["updated_at"] = now
    row["sync_version"] = 1
    row["sync_status"] = "synced"
    return row


async def main():
    suffix = uuid.uuid4().hex[:10]
    extension = _resolve_extension_path()
    assert extension is not None
    shadow = ShadowDB()
    await shadow.initialize(f"/tmp/e2e-production-dispatch-{suffix}.db", extension)
    assert shadow.crr_available
    created = {table: [] for table in TABLES}

    async with AsyncSessionLocal() as db:
        try:
            # Sum strategies.
            for table, quantities in (
                ("branch_inventory", ((3, 1), (4, 2))),
                ("drug_batches", ((3, 2), (4, 3))),
            ):
                base = (await db.execute(text(f"SELECT * FROM {table} LIMIT 1"))).mappings().first()
                if base is None:
                    raise RuntimeError(f"{table} requires one fixture")
                key = {
                    "branch_id": str(base["branch_id"]),
                    "drug_id": str(base["drug_id"]),
                }
                if table == "drug_batches":
                    key["batch_number"] = f"E2E-DISPATCH-{suffix}"
                else:
                    # Choose any real branch/drug pair not already inventoried.
                    unused = (await db.execute(text("""
                        SELECT b.id AS branch_id, d.id AS drug_id
                        FROM branches b JOIN drugs d
                          ON d.organization_id = b.organization_id
                        WHERE NOT EXISTS (
                            SELECT 1 FROM branch_inventory bi
                            WHERE bi.branch_id = b.id AND bi.drug_id = d.id
                        ) LIMIT 1
                    """))).mappings().first()
                    if unused is None:
                        raise RuntimeError("branch_inventory requires an unused drug fixture")
                    key["branch_id"] = str(unused["branch_id"])
                    key["drug_id"] = str(unused["drug_id"])
                ids = [str(uuid.uuid4()), str(uuid.uuid4())]
                created[table].extend(ids)
                for index, row_id in enumerate(ids):
                    quantity, remaining = quantities[index]
                    updates = {"quantity": quantity, "reserved_quantity": 0}
                    if table == "drug_batches":
                        updates = {"quantity": quantity, "remaining_quantity": remaining}
                    row = await _shadow_row(shadow, db, table, row_id, key, updates)
                    await shadow.upsert_merged_row(db, table, row)
                    await db.commit()
                winner = (await db.execute(text(
                    f"SELECT quantity{', remaining_quantity' if table == 'drug_batches' else ''} "
                    f"FROM {table} WHERE id = :id"
                ), {"id": ids[0]})).first()
                assert winner.quantity == 7
                if table == "drug_batches":
                    assert winner.remaining_quantity == 5
                assert (await db.execute(text(f"SELECT 1 FROM {table} WHERE id=:id"), {"id": ids[1]})).first() is None
                print(f"PASS {table}: production sum_and_merge")

            # Keep-both strategies.
            for table, key_column, scope_column in (
                ("prescriptions", "prescription_number", "organization_id"),
                ("purchase_orders", "po_number", "branch_id"),
                ("sales", "sale_number", "branch_id"),
            ):
                base = (await db.execute(text(f"SELECT * FROM {table} LIMIT 1"))).mappings().first()
                if base is None:
                    raise RuntimeError(f"{table} requires one fixture")
                business_key = f"E2E-{table[:4].upper()}-{suffix}"
                key = {scope_column: str(base[scope_column]), key_column: business_key}
                ids = [str(uuid.uuid4()), str(uuid.uuid4())]
                created[table].extend(ids)
                for row_id in ids:
                    row = await _shadow_row(shadow, db, table, row_id, key)
                    await shadow.upsert_merged_row(db, table, row)
                    await db.commit()
                keys = (await db.execute(text(
                    f'SELECT "{key_column}" FROM {table} WHERE id IN (:a,:b) ORDER BY "{key_column}"'
                ), {"a": ids[0], "b": ids[1]})).scalars().all()
                assert keys == [business_key, f"{business_key}-B"]
                audit = (await db.execute(text("""
                    SELECT event_id, old_business_key, new_business_key
                    FROM crr_renumber_audit WHERE loser_id=:loser
                """), {"loser": ids[1]})).mappings().one()
                assert audit["event_id"] and audit["old_business_key"] == business_key
                assert audit["new_business_key"] == f"{business_key}-B"
                print(f"PASS {table}: production keep_both_renumber + audit")
        finally:
            for table, ids in created.items():
                if ids:
                    await db.execute(text(f"DELETE FROM {table} WHERE id IN :ids").bindparams(
                        __import__("sqlalchemy").bindparam("ids", expanding=True)
                    ), {"ids": ids})
            all_ids = [item for ids in created.values() for item in ids]
            if all_ids:
                await db.execute(text("DELETE FROM crr_renumber_audit WHERE winner_id IN :ids OR loser_id IN :ids").bindparams(
                    __import__("sqlalchemy").bindparam("ids", expanding=True)
                ), {"ids": all_ids})
            await db.commit()
            if shadow._conn is not None:
                shadow._conn.close()
            Path(f"/tmp/e2e-production-dispatch-{suffix}.db").unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
