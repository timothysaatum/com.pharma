"""
CRR Sync — prescriptions End-to-End (keep_both_renumber)
=========================================================

Verifies the ``keep_both_renumber`` strategy for ``prescriptions``:

  1. Postgres upsert (ON CONFLICT)
  2. Duplicate business-key detection → BOTH rows survive, loser renumbered
  3. Audit log entry created
  4. Type coercion

Requires:
  - Running Postgres
  - Existing rows in ``branches``, ``customers``, ``organizations``
  - crsqlite.so available

Usage:
    CRSQLITE_EXTENSION_PATH=/path/to/crsqlite.so python3.12 tests/e2e_crr_prescriptions.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import sys
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional

import asyncpg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("e2e_crr_prescriptions")

TEST_PREFIX = "e2e-crr-rx-"
PG_ORG_ID: str = ""
PG_BRANCH_ID: str = ""
PG_CUSTOMER_ID: str = ""

PG_COLUMNS = [
    "id", "organization_id", "branch_id", "prescription_number",
    "customer_id", "prescriber_name", "prescriber_license",
    "prescriber_phone", "prescriber_address", "issue_date",
    "expiry_date", "medications", "diagnosis", "notes",
    "special_instructions", "refills_allowed", "refills_remaining",
    "last_refill_date", "status", "verified_by", "verified_at",
    "created_at", "updated_at", "sync_version", "sync_status",
    "last_synced_at", "sync_hash",
]


def _crsqlite_platform_dir() -> Optional[str]:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "linux-x86_64"
    if machine in {"aarch64", "arm64"}:
        return "linux-aarch64"
    return None


def _find_extension() -> Optional[str]:
    if val := os.environ.get("CRSQLITE_EXTENSION_PATH"):
        if os.path.exists(val):
            return val
    platform_dir = _crsqlite_platform_dir()
    if not platform_dir:
        return None
    repo_root = Path(__file__).resolve().parents[2]
    candidates = [
        repo_root / "crsqlite" / platform_dir / "crsqlite.so",
        Path("crsqlite") / platform_dir / "crsqlite.so",
        Path("..") / "crsqlite" / platform_dir / "crsqlite.so",
    ]
    for p in candidates:
        if p.exists():
            return str(p.resolve())
    return None


async def pg_connect() -> asyncpg.Connection:
    url = os.environ.get(
        "DATABASE_URL",
        "postgresql://vermithor1:laso_dev_2024@localhost:5432/laso_db",
    )
    return await asyncpg.connect(url.replace("+asyncpg", ""))


def _pgify_val(key: str, val: Any) -> Any:
    if val is None:
        return None
    if key in ("updated_at", "created_at", "last_synced_at", "verified_at"):
        if isinstance(val, str) and val.strip():
            try:
                return datetime.fromisoformat(val.replace("Z", "+00:00"))
            except ValueError:
                pass
        return None
    if key in ("issue_date", "expiry_date", "last_refill_date"):
        if isinstance(val, str) and val.strip():
            try:
                return datetime.fromisoformat(val.replace("Z", "+00:00")).date()
            except ValueError:
                pass
        return None
    if key in ("refills_allowed", "refills_remaining", "sync_version"):
        try:
            return int(val)
        except (TypeError, ValueError):
            return 0
    return val


async def _pg_upsert(
    pg: asyncpg.Connection,
    row: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Upsert with keep_both_renumber detection.

    Returns renumbering info if a collision was resolved, else None.
    """
    vals: Dict[str, Any] = {}
    for col in PG_COLUMNS:
        raw = row.get(col)
        if col == "sync_status" and raw is None:
            raw = "synced"
        vals[col] = _pgify_val(col, raw)

    # Duplicate detection by organization_id + prescription_number
    org_id = vals.get("organization_id")
    rx_num = vals.get("prescription_number")
    new_id = vals.get("id")
    if org_id and rx_num and new_id:
        existing = await pg.fetchrow(
            "SELECT id, prescription_number FROM prescriptions "
            "WHERE organization_id = $1 AND prescription_number = $2 AND id != $3",
            org_id, rx_num, new_id,
        )
        if existing is not None:
            existing_id = existing["id"]
            existing_old_key = existing["prescription_number"]
            new_bk = await _disambiguate_bk(rx_num, pg)
            vals["prescription_number"] = new_bk

            # Insert/update the incoming row with disambiguated key
            cols = [c for c in PG_COLUMNS if vals.get(c) is not None]
            col_list = ", ".join(cols)
            ph = ", ".join(f"${i+1}" for i in range(len(cols)))
            updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "id")
            values = [vals[c] for c in cols]
            await pg.execute(
                f"INSERT INTO prescriptions ({col_list}) "
                f"VALUES ({ph}) "
                f"ON CONFLICT (id) DO UPDATE SET {updates}",
                *values,
            )

            # Write audit log
            await pg.execute(
                """INSERT INTO crr_renumber_audit
                   (table_name, winner_id, loser_id, business_key_col,
                    old_business_key, new_business_key)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                "prescriptions", existing_id, new_id,
                "prescription_number", rx_num, new_bk,
            )

            result = {
                "winner_id": existing_id,
                "loser_id": new_id,
                "old_bk": rx_num,
                "new_bk": new_bk,
            }
            logger.info(
                "  🔄 Renumbered %s -> %s (winner=%s, loser=%s)",
                rx_num, new_bk, existing_id[:8], new_id[:8],
            )
            return result

    # Normal upsert
    cols = [c for c in PG_COLUMNS if vals.get(c) is not None]
    if not cols:
        return None
    col_list = ", ".join(cols)
    ph = ", ".join(f"${i+1}" for i in range(len(cols)))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "id")
    values = [vals[c] for c in cols]
    await pg.execute(
        f"INSERT INTO prescriptions ({col_list}) "
        f"VALUES ({ph}) "
        f"ON CONFLICT (id) DO UPDATE SET {updates}",
        *values,
    )
    return None


async def _disambiguate_bk(bk: str, pg: asyncpg.Connection) -> str:
    """Find the next available suffix (B, C, D, ...)."""
    for suffix in "BCDEFGHIJKLMNOPQRSTUVWXYZ":
        candidate = f"{bk}-{suffix}"
        exists = await pg.fetchval(
            "SELECT 1 FROM prescriptions WHERE prescription_number = $1 LIMIT 1",
            candidate,
        )
        if not exists:
            return candidate
    raise RuntimeError(f"Cannot disambiguate {bk}")


async def _cleanup(pg: asyncpg.Connection) -> None:
    await pg.execute(
        "DELETE FROM prescriptions WHERE id LIKE $1", f"{TEST_PREFIX}%"
    )
    await pg.execute(
        "DELETE FROM crr_renumber_audit WHERE winner_id LIKE $1", f"{TEST_PREFIX}%"
    )


# ── Scenarios ─────────────────────────────────────────────────────────

async def test_upsert(pg: asyncpg.Connection) -> None:
    logger.info("=" * 60)
    logger.info("SCENARIO 1: Postgres upsert of merged prescription row")
    logger.info("=" * 60)

    row_id = f"{TEST_PREFIX}upsert-{uuid.uuid4().hex[:8]}"
    now = "2026-07-10T12:00:00Z"

    row = {
        "id": row_id,
        "organization_id": PG_ORG_ID,
        "branch_id": PG_BRANCH_ID,
        "prescription_number": "RX-NORMAL-001",
        "customer_id": PG_CUSTOMER_ID,
        "prescriber_name": "Dr. Smith",
        "prescriber_license": "LIC-12345",
        "issue_date": "2026-07-10T00:00:00Z",
        "expiry_date": "2026-08-10T00:00:00Z",
        "medications": '[{"name": "Amoxicillin", "dosage": "500mg"}]',
        "status": "active",
        "refills_allowed": 2,
        "refills_remaining": 2,
        "sync_version": 1,
        "sync_status": "synced",
        "updated_at": now,
        "created_at": now,
    }
    await _pg_upsert(pg, row)

    pg_row = await pg.fetchrow(
        "SELECT prescription_number, prescriber_name, status "
        "FROM prescriptions WHERE id = $1", row_id
    )
    assert pg_row is not None
    assert pg_row["prescription_number"] == "RX-NORMAL-001"
    assert pg_row["prescriber_name"] == "Dr. Smith"
    logger.info("  ✅ ON CONFLICT upsert: RX-NORMAL-001, Dr. Smith")
    logger.info("  ✅ Scenario 1 PASSED\n")


async def test_keep_both_renumber(pg: asyncpg.Connection) -> None:
    logger.info("=" * 60)
    logger.info("SCENARIO 2: Duplicate business-key — keep_both_renumber")
    logger.info("=" * 60)

    id_a = f"{TEST_PREFIX}dup-A-{uuid.uuid4().hex[:8]}"
    id_b = f"{TEST_PREFIX}dup-B-{uuid.uuid4().hex[:8]}"
    now = "2026-07-10T12:00:00Z"

    # Row A — first client's prescription
    row_a = {
        "id": id_a,
        "organization_id": PG_ORG_ID,
        "branch_id": PG_BRANCH_ID,
        "prescription_number": "RX-COLLIDE",
        "customer_id": PG_CUSTOMER_ID,
        "prescriber_name": "Dr. Alice",
        "prescriber_license": "LIC-AAA",
        "issue_date": "2026-07-10T00:00:00Z",
        "expiry_date": "2026-08-10T00:00:00Z",
        "medications": '[{"name": "Drug A"}]',
        "status": "active",
        "refills_allowed": 1,
        "refills_remaining": 1,
        "sync_version": 1,
        "sync_status": "synced",
        "updated_at": now,
        "created_at": now,
    }
    await _pg_upsert(pg, row_a)
    a = await pg.fetchrow(
        "SELECT prescription_number, prescriber_name FROM prescriptions WHERE id = $1", id_a
    )
    assert a is not None
    assert a["prescription_number"] == "RX-COLLIDE"
    logger.info("  ✅ Row A inserted: RX-COLLIDE, Dr. Alice")

    # Row B — same prescription_number, different id → should get renumbered
    row_b = {
        "id": id_b,
        "organization_id": PG_ORG_ID,
        "branch_id": PG_BRANCH_ID,
        "prescription_number": "RX-COLLIDE",
        "customer_id": PG_CUSTOMER_ID,
        "prescriber_name": "Dr. Bob",
        "prescriber_license": "LIC-BBB",
        "issue_date": "2026-07-10T00:00:00Z",
        "expiry_date": "2026-08-10T00:00:00Z",
        "medications": '[{"name": "Drug B"}]',
        "status": "active",
        "refills_allowed": 0,
        "refills_remaining": 0,
        "sync_version": 1,
        "sync_status": "synced",
        "updated_at": now,
        "created_at": now,
    }
    result = await _pg_upsert(pg, row_b)
    assert result is not None, "Renumbering should have occurred"
    assert result["winner_id"] == id_a
    assert result["loser_id"] == id_b
    assert result["old_bk"] == "RX-COLLIDE"
    assert result["new_bk"] == "RX-COLLIDE-B"
    logger.info("  ✅ Renumbered: RX-COLLIDE -> RX-COLLIDE-B")

    # Verify — BOTH rows exist
    count = await pg.fetchval(
        "SELECT COUNT(*) FROM prescriptions "
        "WHERE organization_id = $1 AND id IN ($2, $3)",
        PG_ORG_ID, id_a, id_b,
    )
    assert count == 2, f"Expected 2 rows, got {count}"
    logger.info("  ✅ Both rows survive in Postgres")

    # Verify row A unchanged
    a2 = await pg.fetchrow(
        "SELECT prescription_number, prescriber_name FROM prescriptions WHERE id = $1", id_a
    )
    assert a2["prescription_number"] == "RX-COLLIDE"
    assert a2["prescriber_name"] == "Dr. Alice"
    logger.info("  ✅ Winner (A) unchanged: RX-COLLIDE, Dr. Alice")

    # Verify row B has renumbered key + original data intact
    b2 = await pg.fetchrow(
        "SELECT prescription_number, prescriber_name FROM prescriptions WHERE id = $1", id_b
    )
    assert b2 is not None, "Loser (B) should still exist"
    assert b2["prescription_number"] == "RX-COLLIDE-B"
    assert b2["prescriber_name"] == "Dr. Bob"
    logger.info("  ✅ Loser (B) renumbered to RX-COLLIDE-B, data intact")

    # Verify audit log
    audit = await pg.fetchrow(
        "SELECT table_name, winner_id, loser_id, business_key_col, "
        "old_business_key, new_business_key "
        "FROM crr_renumber_audit "
        "WHERE loser_id = $1 ORDER BY renumbered_at DESC LIMIT 1",
        id_b,
    )
    assert audit is not None, "Audit log entry should exist"
    assert audit["table_name"] == "prescriptions"
    assert audit["winner_id"] == id_a
    assert audit["loser_id"] == id_b
    assert audit["business_key_col"] == "prescription_number"
    assert audit["old_business_key"] == "RX-COLLIDE"
    assert audit["new_business_key"] == "RX-COLLIDE-B"
    logger.info("  ✅ Audit log correct: prescriptions, RX-COLLIDE -> RX-COLLIDE-B")

    logger.info("  ✅ Scenario 2 PASSED\n")


async def test_type_coercion(pg: asyncpg.Connection) -> None:
    logger.info("=" * 60)
    logger.info("SCENARIO 3: Type coercion")
    logger.info("=" * 60)

    row_id = f"{TEST_PREFIX}type-{uuid.uuid4().hex[:8]}"
    now = "2026-07-10T12:00:00Z"

    row = {
        "id": row_id,
        "organization_id": PG_ORG_ID,
        "branch_id": PG_BRANCH_ID,
        "prescription_number": "RX-TYPE",
        "customer_id": PG_CUSTOMER_ID,
        "prescriber_name": "Dr. Type",
        "prescriber_license": "LIC-TYPE",
        "issue_date": "2026-07-10T00:00:00Z",
        "expiry_date": "2026-08-10T00:00:00Z",
        "medications": "[]",
        "refills_allowed": 3,
        "refills_remaining": 2,
        "status": "active",
        "sync_version": 1,
        "sync_status": "synced",
        "updated_at": now,
        "created_at": now,
    }
    await _pg_upsert(pg, row)

    pg_row = await pg.fetchrow(
        "SELECT issue_date, expiry_date, refills_allowed, refills_remaining "
        "FROM prescriptions WHERE id = $1", row_id
    )
    assert pg_row is not None
    assert isinstance(pg_row["issue_date"], (date, datetime))
    assert isinstance(pg_row["expiry_date"], (date, datetime))
    assert int(pg_row["refills_allowed"]) == 3
    assert int(pg_row["refills_remaining"]) == 2
    logger.info("  ✅ dates → timestamptz, integers → int")
    logger.info("  ✅ Scenario 3 PASSED\n")


# ── Main ──────────────────────────────────────────────────────────────

async def main():
    ext_path = _find_extension()
    if not ext_path:
        logger.info("cr-sqlite extension not required for PG-only test")

    pg = await pg_connect()
    logger.info("Postgres OK (%s)", (await pg.fetchval("SELECT version()")).split(",")[0])

    global PG_ORG_ID, PG_BRANCH_ID, PG_CUSTOMER_ID
    PG_BRANCH_ID = await pg.fetchval(
        "SELECT id FROM branches ORDER BY created_at LIMIT 1"
    )
    PG_ORG_ID = await pg.fetchval(
        "SELECT organization_id FROM branches WHERE id = $1", PG_BRANCH_ID
    )
    PG_CUSTOMER_ID = await pg.fetchval(
        "SELECT id FROM customers ORDER BY created_at LIMIT 1"
    )
    logger.info(
        "FK parents: org=%s branch=%s customer=%s",
        PG_ORG_ID[:8], PG_BRANCH_ID[:8], (PG_CUSTOMER_ID or "n/a")[:8] if PG_CUSTOMER_ID else "n/a",
    )

    try:
        await _cleanup(pg)
        await test_upsert(pg)
        await _cleanup(pg)
        await test_keep_both_renumber(pg)
        await _cleanup(pg)
        await test_type_coercion(pg)
        await _cleanup(pg)

        logger.info("=" * 60)
        logger.info("  ALL prescriptions SCENARIOS PASSED ✅")
        logger.info("=" * 60)

    finally:
        await _cleanup(pg)
        await pg.close()


if __name__ == "__main__":
    asyncio.run(main())
