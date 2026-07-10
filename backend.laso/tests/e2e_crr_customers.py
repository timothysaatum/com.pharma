"""Real-Postgres CRR customer dedup verification using production ShadowDB code.

Scenarios:
1. Three-way OR-match on a non-empty phone; earliest customer survives.
2. Loyalty points sum across all three, newest non-null fields win, and conflicts are audited.
3. Sale and prescription references are repointed to the survivor.
4. Two customers with empty phone and email remain distinct.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import bindparam, text

from app.db.session import AsyncSessionLocal
from app.services.sync.shadow_db import ShadowDB


TEST_PREFIX = "e2e-crr-customer-"


async def _clone_row(db, table: str, source_id: str, overrides: dict) -> None:
    columns = (await db.execute(text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = :table
        ORDER BY ordinal_position
    """), {"table": table})).scalars().all()
    expressions = []
    params = {"source_id": source_id}
    for column in columns:
        if column in overrides:
            param = f"override_{column}"
            expressions.append(f":{param}")
            params[param] = overrides[column]
        else:
            expressions.append(f'"{column}"')
    await db.execute(text(f"""
        INSERT INTO {table} ({', '.join(f'"{column}"' for column in columns)})
        SELECT {', '.join(expressions)} FROM {table} WHERE id = :source_id
    """), params)


def _shadow_customer(row: dict) -> dict:
    columns = {
        "id", "organization_id", "customer_type", "first_name", "last_name",
        "phone", "email", "date_of_birth", "loyalty_points", "loyalty_tier",
        "insurance_provider_id", "insurance_member_id", "preferred_contract_id",
        "is_active", "is_deleted", "sync_status", "sync_version",
        "updated_at", "created_at",
    }
    result = {column: row.get(column) for column in columns}
    for column in ("created_at", "updated_at", "date_of_birth"):
        if result.get(column) is not None:
            result[column] = result[column].isoformat()
    return result


async def main() -> None:
    suffix = uuid.uuid4().hex[:10]
    ids = {
        name: str(uuid.uuid4()) for name in (
            "survivor", "loser", "third", "sale", "prescription", "blank_a", "blank_b"
        )
    }
    phone = f"+233{suffix[:9]}"
    now = datetime.now(timezone.utc)
    shadow_path = f"/tmp/{TEST_PREFIX}{suffix}.db"
    extension = str(
        Path(__file__).parents[2] / "ui.laso" / "src-tauri" / "crsqlite.so"
    )
    shadow = ShadowDB()
    await shadow.initialize(shadow_path, extension)

    async with AsyncSessionLocal() as db:
        fixture = (await db.execute(text("""
            SELECT p.id AS prescription_id, s.id AS sale_id, p.customer_id,
                   p.organization_id, p.branch_id
            FROM prescriptions p
            JOIN sales s ON s.organization_id = p.organization_id
                        AND s.branch_id = p.branch_id
            WHERE p.customer_id IS NOT NULL
            LIMIT 1
        """))).mappings().first()
        if fixture is None:
            raise RuntimeError("Real Postgres fixture requires a sale and prescription in one branch")

        try:
            await _clone_row(db, "customers", fixture["customer_id"], {
                "id": ids["survivor"], "phone": phone,
                "email": f"early-{suffix}@example.test", "first_name": "Early",
                "insurance_member_id": None, "loyalty_points": 5,
                "created_at": now - timedelta(days=2),
                "updated_at": now - timedelta(days=2),
            })
            await _clone_row(db, "customers", fixture["customer_id"], {
                "id": ids["loser"], "phone": phone,
                "email": f"new-{suffix}@example.test", "first_name": "Newest",
                "insurance_member_id": f"MEM-{suffix}", "loyalty_points": 7,
                "created_at": now - timedelta(days=1), "updated_at": now,
            })
            await _clone_row(db, "customers", fixture["customer_id"], {
                "id": ids["third"], "phone": phone,
                "email": f"third-{suffix}@example.test", "first_name": "Third",
                "insurance_member_id": None, "loyalty_points": 11,
                "created_at": now - timedelta(hours=12),
                "updated_at": now + timedelta(minutes=1),
            })
            await _clone_row(db, "sales", fixture["sale_id"], {
                "id": ids["sale"], "customer_id": ids["loser"],
                "sale_number": f"{TEST_PREFIX}SALE-{suffix}",
            })
            await _clone_row(db, "prescriptions", fixture["prescription_id"], {
                "id": ids["prescription"], "customer_id": ids["loser"],
                "prescription_number": f"{TEST_PREFIX}RX-{suffix}",
            })
            await db.commit()

            incoming = dict((await db.execute(
                text("SELECT * FROM customers WHERE id = :id"), {"id": ids["third"]}
            )).mappings().one())
            await shadow.upsert_merged_row(
                db, "customers", _shadow_customer(incoming)
            )
            await db.commit()

            survivor = (await db.execute(text(
                "SELECT * FROM customers WHERE id = :id"
            ), {"id": ids["survivor"]})).mappings().one()
            loser_count = await db.scalar(
                text("SELECT COUNT(*) FROM customers WHERE id IN :ids").bindparams(
                    bindparam("ids", expanding=True)
                ), {"ids": [ids["loser"], ids["third"]]}
            )
            sale_customer = await db.scalar(text(
                "SELECT customer_id FROM sales WHERE id = :id"
            ), {"id": ids["sale"]})
            prescription_customer = await db.scalar(text(
                "SELECT customer_id FROM prescriptions WHERE id = :id"
            ), {"id": ids["prescription"]})
            audits = (await db.execute(text("""
                SELECT * FROM crr_customer_merge_audit
                WHERE survivor_id = :survivor ORDER BY loser_id
            """), {"survivor": ids["survivor"]})).mappings().all()
            audit = next(row for row in audits if row["loser_id"] == ids["loser"])

            assert loser_count == 0
            assert int(survivor["loyalty_points"]) == 23
            assert survivor["first_name"] == "Third"
            assert survivor["insurance_member_id"] == f"MEM-{suffix}"
            assert str(sale_customer) == ids["survivor"]
            assert str(prescription_customer) == ids["survivor"]
            assert {item["field"] for item in audit["matched_fields"]} == {"phone"}
            resolutions = {item["field"]: item for item in audit["field_resolutions"]}
            assert resolutions["loyalty_points"]["resolution"] == "sum"
            assert resolutions["loyalty_points"]["result"] == 23
            assert resolutions["first_name"]["resolution"] == "newest_non_null"
            assert len(audits) == 2
            print("PASS three-way customer collision: one survivor, all fields folded, two audits")

            # Empty contacts must never be treated as a business key.
            for key, created in (("blank_a", now - timedelta(hours=2)), ("blank_b", now - timedelta(hours=1))):
                await _clone_row(db, "customers", ids["survivor"], {
                    "id": ids[key], "phone": None, "email": None,
                    "first_name": key, "loyalty_points": 0,
                    "created_at": created, "updated_at": created,
                })
            await db.commit()
            blank_b = dict((await db.execute(text(
                "SELECT * FROM customers WHERE id = :id"
            ), {"id": ids["blank_b"]})).mappings().one())
            await shadow.upsert_merged_row(db, "customers", _shadow_customer(blank_b))
            await db.commit()
            blank_count = await db.scalar(
                text("SELECT COUNT(*) FROM customers WHERE id IN :ids").bindparams(
                    bindparam("ids", expanding=True)
                ), {"ids": [ids["blank_a"], ids["blank_b"]]}
            )
            assert blank_count == 2
            print("PASS empty phone/email: distinct walk-ins preserved")
            print("ALL customer CRR scenarios passed against real Postgres")
        finally:
            await db.rollback()
            await db.execute(text("DELETE FROM sales WHERE id = :id"), {"id": ids["sale"]})
            await db.execute(text("DELETE FROM prescriptions WHERE id = :id"), {"id": ids["prescription"]})
            await db.execute(text(
                "DELETE FROM crr_customer_merge_audit WHERE survivor_id IN :ids OR loser_id IN :ids"
            ).bindparams(bindparam("ids", expanding=True)), {"ids": list(ids.values())})
            await db.execute(text("DELETE FROM customers WHERE id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ), {"ids": list(ids.values())})
            await db.commit()


if __name__ == "__main__":
    asyncio.run(main())
