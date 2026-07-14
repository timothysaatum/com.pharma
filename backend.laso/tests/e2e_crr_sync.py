"""
CRR Sync — End-to-End Simulation
=================================

Tests the full cr-sqlite CRDT sync pipeline WITHOUT a running Postgres
or FastAPI server:

  ┌──────────┐    ┌──────────────┐    ┌──────────┐
  │ Client A │───▶│  Shadow DB   │───▶│ Postgres │
  │ (SQLite) │    │  (cr-sqlite) │    │ (SQLite  │
  │          │    │              │    │  stand-in)│
  │ Client B │───▶│              │───▶│          │
  └──────────┘    └──────────────┘    └──────────┘

- Postgres is simulated by a second SQLite file with the UNIQUE(branch_id, drug_id)
  constraint to test the duplicate business-key resolution.
- Shadow DB uses cr-sqlite for CRDT merge.
- Client A and B are SQLite files with cr-sqlite loaded.

Scenarios:
  1. Field-level merge: A edits quantity, B edits location (same row)
  2. Duplicate business-key: A and B create rows with same branch_id+drug_id
  3. Crash recovery: Kill mid-upsert, verify reconcile_table() recovers
  4. Blob serialisation roundtrip (b64 prefix)

Usage:
    python3.12 tests/e2e_crr_sync.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import platform
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("e2e_crr_sync")

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _crsqlite_platform_dir() -> Optional[str]:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "linux-x86_64"
    if machine in {"aarch64", "arm64"}:
        return "linux-aarch64"
    return None


# Resolve crsqlite.so path (same logic as shadow_db.py)
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


def _sqlite_load_path(extension: str) -> str:
    for suffix in (".so", ".dylib", ".dll"):
        if extension.endswith(suffix):
            return extension[: -len(suffix)]
    return extension


def create_sqlite_db(path: str, schema: str, load_crsqlite: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(schema)
    if load_crsqlite:
        ext = _find_extension()
        if ext:
            conn.enable_load_extension(True)
            conn.load_extension(_sqlite_load_path(ext))
            conn.execute("SELECT crsql_as_crr('branch_inventory')")
    conn.commit()
    return conn


# ── Schemas ──────────────────────────────────────────────────────────────────

SHADOW_SCHEMA = """
    CREATE TABLE IF NOT EXISTS branch_inventory (
        id                TEXT NOT NULL PRIMARY KEY,
        branch_id         TEXT NOT NULL DEFAULT '',
        drug_id           TEXT NOT NULL DEFAULT '',
        quantity          INTEGER NOT NULL DEFAULT 0,
        reserved_quantity INTEGER NOT NULL DEFAULT 0,
        location          TEXT,
        selling_price     REAL,
        sync_status       TEXT NOT NULL DEFAULT 'synced',
        sync_version      INTEGER NOT NULL DEFAULT 1,
        synced_at         TEXT,
        updated_at        TEXT NOT NULL DEFAULT '',
        created_at        TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_branch_inventory_lookup
        ON branch_inventory(branch_id, drug_id);
"""

POSTGRES_STANDIN_SCHEMA = """
    CREATE TABLE IF NOT EXISTS branch_inventory (
        id                TEXT NOT NULL PRIMARY KEY,
        branch_id         TEXT NOT NULL DEFAULT '',
        drug_id           TEXT NOT NULL DEFAULT '',
        quantity          INTEGER NOT NULL DEFAULT 0,
        reserved_quantity INTEGER NOT NULL DEFAULT 0,
        location          TEXT,
        selling_price     REAL,
        sync_status       TEXT NOT NULL DEFAULT 'synced',
        sync_version      INTEGER NOT NULL DEFAULT 1,
        synced_at         TEXT,
        updated_at        TEXT NOT NULL DEFAULT '',
        created_at        TEXT NOT NULL DEFAULT '',
        UNIQUE(branch_id, drug_id)
    );
"""

# Same as localDb.ts migration v1 (no UNIQUE constraint, NOT NULL PK)
CLIENT_SCHEMA = """
    CREATE TABLE IF NOT EXISTS branch_inventory (
        id                TEXT NOT NULL PRIMARY KEY,
        branch_id         TEXT NOT NULL DEFAULT '',
        drug_id           TEXT NOT NULL DEFAULT '',
        quantity          INTEGER NOT NULL DEFAULT 0,
        reserved_quantity INTEGER NOT NULL DEFAULT 0,
        location          TEXT,
        selling_price     REAL,
        sync_status       TEXT NOT NULL DEFAULT 'synced',
        sync_version      INTEGER NOT NULL DEFAULT 1,
        synced_at         TEXT,
        updated_at        TEXT NOT NULL DEFAULT '',
        created_at        TEXT NOT NULL DEFAULT ''
    );
"""

# ──────────────────────────────────────────────────────────────────────────────
# Scenario 1: Field-level merge
# ──────────────────────────────────────────────────────────────────────────────

async def test_field_level_merge(
    shadow: sqlite3.Connection,
    pg: sqlite3.Connection,
    ext_path: Optional[str],
) -> None:
    """
    Client A and Client B start from the same synced row.
    A edits quantity, B edits location (different fields, same row).
    After both push → shadow merge → Postgres upsert, both edits survive.
    """
    logger.info("=" * 60)
    logger.info("SCENARIO 1: Field-level merge (quantity + location)")
    logger.info("=" * 60)

    row_id = str(uuid.uuid4())
    created_at = "2026-07-10T00:00:00Z"
    base_row = dict(
        id=row_id, branch_id="branch-A", drug_id="drug-1",
        quantity=100, reserved_quantity=0,
        location="Shelf-1", selling_price=None,
        sync_status="synced", sync_version=1, synced_at=None,
        updated_at=created_at, created_at=created_at,
    )

    # Simulate initial sync: both clients have the same base row
    for client_label in ("A", "B"):
        client_db_path = f"/tmp/e2e_client_{client_label}.db"
        if os.path.exists(client_db_path):
            os.remove(client_db_path)
        conn = create_sqlite_db(client_db_path, CLIENT_SCHEMA, load_crsqlite=True)
        conn.execute(
            "INSERT INTO branch_inventory (id, branch_id, drug_id, quantity, "
            "reserved_quantity, location, selling_price, sync_status, sync_version, "
            "synced_at, updated_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(base_row.values()),
        )
        conn.commit()
        conn.close()

    # Client A changes quantity from 100 → 150
    a_conn = create_sqlite_db(f"/tmp/e2e_client_A.db", CLIENT_SCHEMA, load_crsqlite=True)
    a_conn.execute("UPDATE branch_inventory SET quantity = 150 WHERE id = ?", (row_id,))
    a_conn.commit()
    a_changes = _get_local_changes(a_conn, "site-A")
    a_conn.close()

    # Client B changes location from "Shelf-1" → "Bin-3"
    b_conn = create_sqlite_db(f"/tmp/e2e_client_B.db", CLIENT_SCHEMA, load_crsqlite=True)
    b_conn.execute("UPDATE branch_inventory SET location = 'Bin-3' WHERE id = ?", (row_id,))
    b_conn.commit()
    b_changes = _get_local_changes(b_conn, "site-B")
    b_conn.close()

    # Server push handling: insert into shadow
    _insert_into_shadow(shadow, a_changes)
    _insert_into_shadow(shadow, b_changes)

    # Read merged row
    merged = _read_shadow_row(shadow, row_id)
    assert merged is not None, "Merged row should exist"
    logger.info("  Merged row: quantity=%s location=%s", merged["quantity"], merged["location"])

    # Verify field-level merge: both edits preserved
    assert int(merged["quantity"]) == 150, f"Expected quantity=150, got {merged['quantity']}"
    assert merged["location"] == "Bin-3", f"Expected location='Bin-3', got {merged['location']}"
    logger.info("  ✅ Field-level merge CORRECT — quantity=150, location='Bin-3'")

    # Now upsert to Postgres stand-in
    _upsert_to_pg(pg, merged, shadow)
    pg_row = _read_pg_row(pg, row_id)
    assert int(pg_row["quantity"]) == 150
    assert pg_row["location"] == "Bin-3"
    logger.info("  ✅ Postgres upsert correct — both fields preserved")

    # Cleanup
    for label in ("A", "B"):
        try:
            os.remove(f"/tmp/e2e_client_{label}.db")
        except OSError:
            pass

    logger.info("  ✅ Scenario 1 PASSED\n")


# ──────────────────────────────────────────────────────────────────────────────
# Scenario 2: Duplicate business-key resolution
# ──────────────────────────────────────────────────────────────────────────────

async def test_duplicate_business_key(
    shadow: sqlite3.Connection,
    pg: sqlite3.Connection,
) -> None:
    """
    Client A and Client B each create a new row for the same branch_id+drug_id
    while offline (different id, same business key).  After both push:
    1. Shadow DB gets both rows (cr-sqlite tracks each as separate)
    2. Server detects (branch_id, drug_id) collision during Postgres upsert
    3. Server merges the rows (sum quantities, keep latest metadata)
    4. The survivor is upserted, the duplicate is removed from shadow DB
    """
    logger.info("=" * 60)
    logger.info("SCENARIO 2: Duplicate business-key resolution")
    logger.info("=" * 60)

    branch_id = "branch-A"
    drug_id = "drug-X"
    id_a = str(uuid.uuid4())
    id_b = str(uuid.uuid4())

    # Fresh shadow DB row set
    now = "2026-07-10T12:00:00Z"
    shadow.executescript("DELETE FROM branch_inventory; DELETE FROM crsql_changes")
    shadow.commit()

    # ── Push Client A's new row ──────────────────────────────────────
    logger.info("  [Push A] id=%s qty=50 location='Shelf-A'", id_a[:8])
    def _push_row(conn, row_id, branch_id, drug_id, qty, loc, ts):
        conn.execute(
            "INSERT INTO branch_inventory (id, branch_id, drug_id, quantity, "
            "reserved_quantity, location, selling_price, sync_status, sync_version, "
            "synced_at, updated_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row_id, branch_id, drug_id, qty, 0, loc, None,
             "synced", 1, None, ts, ts),
        )
        conn.commit()
    _push_row(shadow, id_a, branch_id, drug_id, 50, "Shelf-A", now)
    merged_a = _read_shadow_row(shadow, id_a)
    _upsert_to_pg(pg, merged_a, shadow)
    logger.info("  ✅ Client A row in Postgres: id=%s qty=%s", id_a[:8], _read_pg_row(pg, id_a)["quantity"])

    # ── Push Client B's row (different id, same business key) ────────
    logger.info("  [Push B] id=%s qty=30 location='Bin-2' (SAME branch_id+drug_id)", id_b[:8])
    later = "2026-07-10T14:00:00Z"
    _push_row(shadow, id_b, branch_id, drug_id, 30, "Bin-2", later)
    merged_b = _read_shadow_row(shadow, id_b)
    assert merged_b is not None, "Client B's row should exist in shadow"

    # Count rows in shadow before upsert
    cnt = shadow.execute("SELECT COUNT(*) FROM branch_inventory").fetchone()[0]
    logger.info("  Rows in shadow before upsert: %d", cnt)
    assert cnt == 2, f"Expected 2 rows in shadow, got {cnt}"

    # ── Attempt Postgres upsert — should detect collision ────────────
    _upsert_to_pg(pg, merged_b, shadow)

    # Postgres should now have ONE row with merged values
    cnt_pg = pg.execute("SELECT COUNT(*) FROM branch_inventory").fetchone()[0]
    logger.info("  Rows in Postgres after merge: %d", cnt_pg)
    assert cnt_pg == 1, f"Expected 1 row in Postgres after merge, got {cnt_pg}"

    # Verify merged values: quantities summed (50+30=80), location from later client
    survivor_id_shadow = shadow.execute(
        "SELECT id FROM branch_inventory"
    ).fetchone()[0]
    survivor = _read_pg_row(pg, survivor_id_shadow)\
        or _read_pg_row(pg, id_a) or _read_pg_row(pg, id_b)
    assert survivor is not None, "Could not find survivor in Postgres"
    logger.info("  Survivor row: id=%s qty=%s location=%s updated_at=%s",
                survivor["id"][:8], survivor["quantity"], survivor["location"], survivor["updated_at"])
    assert int(survivor["quantity"]) == 80, f"Expected merged quantity=80, got {survivor['quantity']}"
    # Client B was later, so location should be 'Bin-2'
    assert survivor["location"] == "Bin-2", f"Expected location='Bin-2' (newer), got '{survivor['location']}'"
    logger.info("  ✅ Merge CORRECT — qty=50+30=80, location from newer client")

    # Shadow DB should have removed the duplicate
    cnt_shadow_after = shadow.execute("SELECT COUNT(*) FROM branch_inventory").fetchone()[0]
    logger.info("  Rows in shadow after clean-up: %d", cnt_shadow_after)
    assert cnt_shadow_after == 1, f"Expected 1 row in shadow after clean-up, got {cnt_shadow_after}"

    # Verify the surviving row id (should be the original, not the duplicate)
    logger.info("  Survivor id in Postgres: %s", survivor["id"][:8])
    # The existing row (id_a) should survive since it was in Postgres first
    assert survivor["id"] == id_a, f"Expected survivor id={id_a} (first arrival), got {survivor['id']}"
    logger.info("  ✅ Survivor is the first-arrived row (id=%s)", id_a[:8])

    logger.info("  ✅ Scenario 2 PASSED\n")


# ──────────────────────────────────────────────────────────────────────────────
# Scenario 3: Crash recovery (reconcile_table)
# ──────────────────────────────────────────────────────────────────────────────

async def test_crash_recovery(
    shadow: sqlite3.Connection,
    pg: sqlite3.Connection,
) -> None:
    """
    Simulate a server crash between shadow DB merge and Postgres upsert:
    1. Client pushes changes → inserted into shadow DB (committed)
    2. Server crashes before upserting to Postgres
    3. On restart, reconcile_table() re-plays all shadow rows into Postgres
    4. Verify data is consistent after recovery
    """
    logger.info("=" * 60)
    logger.info("SCENARIO 3: Crash recovery (reconcile_table)")
    logger.info("=" * 60)

    # Fresh state
    shadow.executescript("DELETE FROM branch_inventory; DELETE FROM crsql_changes")
    pg.execute("DELETE FROM branch_inventory")
    shadow.commit()
    pg.commit()

    # Simulate a push that was committed to shadow but NOT to Postgres
    crash_id = str(uuid.uuid4())
    now = "2026-07-10T08:00:00Z"
    shadow.execute(
        "INSERT INTO branch_inventory (id, branch_id, drug_id, quantity, "
        "reserved_quantity, location, selling_price, sync_status, sync_version, "
        "synced_at, updated_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (crash_id, "branch-C", "drug-3", 200, 10, "Crash-Shelf", None,
         "synced", 2, None, now, now),
    )
    shadow.commit()

    # Verify: shadow has it, Postgres does NOT
    assert _read_shadow_row(shadow, crash_id) is not None
    assert _read_pg_row(pg, crash_id) is None
    logger.info("  💥 Crash simulated: row in shadow but NOT in Postgres")

    # ── Recovery ─────────────────────────────────────────────────────
    # reconcile_table() replays ALL shadow rows into Postgres
    all_shadow_rows = shadow.execute(
        "SELECT * FROM branch_inventory"
    ).fetchall()
    logger.info("  Rows to reconcile: %d", len(all_shadow_rows))
    assert len(all_shadow_rows) >= 1

    for row in all_shadow_rows:
        row_dict = dict(zip(
            ["id", "branch_id", "drug_id", "quantity", "reserved_quantity",
             "location", "selling_price", "sync_status", "sync_version",
             "synced_at", "updated_at", "created_at"],
            row,
        ))
        _upsert_to_pg(pg, row_dict, shadow)

    pg_crash_row = _read_pg_row(pg, crash_id)
    assert pg_crash_row is not None, "Crash row should exist after recovery"
    assert int(pg_crash_row["quantity"]) == 200
    logger.info("  ✅ Recovery successful: crash_id=%s qty=%s location=%s",
                crash_id[:8], pg_crash_row["quantity"], pg_crash_row["location"])

    # Verify shadow and Postgres are in sync
    shadow_count = shadow.execute("SELECT COUNT(*) FROM branch_inventory").fetchone()[0]
    pg_count = pg.execute("SELECT COUNT(*) FROM branch_inventory").fetchone()[0]
    assert shadow_count == pg_count, (
        f"Shadow ({shadow_count}) and Postgres ({pg_count}) row counts differ"
    )
    logger.info("  ✅ Shadow and Postgres in sync (%d rows each)", shadow_count)

    logger.info("  ✅ Scenario 3 PASSED\n")


# ──────────────────────────────────────────────────────────────────────────────
# Scenario 4: BLOB serialisation roundtrip
# ──────────────────────────────────────────────────────────────────────────────

async def test_blob_serialization() -> None:
    """
    Verify the b64: prefix roundtrip works for bytes pk/val values.
    """
    logger.info("=" * 60)
    logger.info("SCENARIO 4: BLOB serialisation roundtrip")
    logger.info("=" * 60)

    # Simulate what the server does when reading from shadow DB:
    # sqlite3 returns bytes for BLOB columns
    raw_pk = b"\xde\xad\xbe\xef\x00\x01"
    raw_val = b"\xca\xfe\xba\xbe"

    # Server-side serialization (field_serializer in CrrChangeRow)
    def serialize(v):
        if isinstance(v, bytes):
            return "b64:" + base64.b64encode(v).decode("ascii")
        return v

    serialized_pk = serialize(raw_pk)
    serialized_val = serialize(raw_val)
    logger.info("  Raw pk:   %s", raw_pk.hex())
    logger.info("  Serialized pk: %s", serialized_pk)
    logger.info("  Serialized val: %s", serialized_val)

    assert serialized_pk.startswith("b64:"), "Should have b64: prefix"
    assert serialized_val.startswith("b64:"), "Should have b64: prefix"

    # JSON encode/decode (simulates HTTP transport)
    json_str = json.dumps({"pk": serialized_pk, "val": serialized_val})
    decoded = json.loads(json_str)
    assert decoded["pk"] == serialized_pk
    assert decoded["val"] == serialized_val
    logger.info("  ✅ JSON transport OK")

    # Client-side decoding (applyCrrPullChanges logic)
    def deserialize(v):
        if isinstance(v, str) and v.startswith("b64:"):
            return base64.b64decode(v[4:])
        return v

    restored_pk = deserialize(decoded["pk"])
    restored_val = deserialize(decoded["val"])
    assert restored_pk == raw_pk, f"Roundtrip failed: {restored_pk!r} != {raw_pk!r}"
    assert restored_val == raw_val, f"Roundtrip failed: {restored_val!r} != {raw_val!r}"
    logger.info("  ✅ Bytes roundtrip CORRECT")

    # Verify text passthrough (most common case for branch_inventory)
    text_pk = "550e8400-e29b-41d4-a716-446655440000"
    text_val = "Shelf-A"
    assert deserialize(serialize(text_pk)) == text_pk
    assert deserialize(serialize(text_val)) == text_val
    logger.info("  ✅ Text passthrough CORRECT")

    # Integer passthrough
    int_val = 42
    assert deserialize(serialize(int_val)) == int_val
    logger.info("  ✅ Integer passthrough CORRECT")

    # None passthrough
    assert deserialize(serialize(None)) is None
    logger.info("  ✅ None passthrough CORRECT")

    logger.info("  ✅ Scenario 4 PASSED\n")


# ──────────────────────────────────────────────────────────────────────────────
# Scenario 5: Full push/pull roundtrip with real crsql_changes
# ──────────────────────────────────────────────────────────────────────────────

async def test_full_push_pull_roundtrip(
    shadow: sqlite3.Connection,
    pg: sqlite3.Connection,
    ext_path: Optional[str],
) -> None:
    """
    Simulate a complete client push → shadow merge → Postgres upsert →
    server pull → client apply roundtrip using real crsql_changes.
    """
    logger.info("=" * 60)
    logger.info("SCENARIO 5: Full push/pull roundtrip")
    logger.info("=" * 60)

    # Fresh state
    shadow.executescript("DELETE FROM branch_inventory; DELETE FROM crsql_changes")
    pg.execute("DELETE FROM branch_inventory")
    shadow.commit()
    pg.commit()

    row_id = str(uuid.uuid4())
    now = "2026-07-10T10:00:00Z"

    # ── Simulate client creating an insert ────────────────────────────
    # Create a client DB, insert a row, capture the crsql_changes
    client_path = "/tmp/e2e_roundtrip_client.db"
    if os.path.exists(client_path):
        os.remove(client_path)
    client = create_sqlite_db(client_path, CLIENT_SCHEMA, load_crsqlite=True)
    client.execute(
        "INSERT INTO branch_inventory (id, branch_id, drug_id, quantity, "
        "reserved_quantity, location, selling_price, sync_status, sync_version, "
        "synced_at, updated_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (row_id, "branch-R", "drug-R", 75, 5, "Roundtrip-Shelf", None,
         "synced", 1, None, now, now),
    )
    client.commit()

    # Read client's crsql_changes (simulates push)
    client_changes = _get_local_changes(client, "site-R")
    logger.info("  Client crsql_changes count: %d", len(client_changes))
    assert len(client_changes) > 0, "Expected crsql_changes rows"

    # ── Server inserts into shadow ───────────────────────────────────
    _insert_into_shadow(shadow, client_changes)
    merged = _read_shadow_row(shadow, row_id)
    assert merged is not None, "Row should exist in shadow after merge"
    logger.info("  Shadow merged row: qty=%s location=%s", merged["quantity"], merged["location"])

    # ── Upsert to Postgres ────────────────────────────────────────────
    assert merged["branch_id"] == "branch-R"
    _upsert_to_pg(pg, merged, shadow)
    pg_row = _read_pg_row(pg, row_id)
    assert pg_row is not None
    assert int(pg_row["quantity"]) == 75
    assert pg_row["location"] == "Roundtrip-Shelf"
    logger.info("  ✅ Postgres row correct: qty=%s location=%s", pg_row["quantity"], pg_row["location"])

    # ── Server pull: read changes from shadow ─────────────────────────
    server_changes_raw = shadow.execute(
        """SELECT "table", pk, cid, val, col_version, db_version,
                  site_id, cl, seq
           FROM crsql_changes
           ORDER BY seq"""
    ).fetchall()
    logger.info("  Server crsql_changes count: %d", len(server_changes_raw))
    assert len(server_changes_raw) > 0

    # Build the same structure the pull endpoint returns
    pull_columns = ["table", "pk", "cid", "val", "col_version",
                     "db_version", "site_id", "cl", "seq"]
    pull_changes = []
    for raw in server_changes_raw:
        entry = dict(zip(pull_columns, raw))
        # Simulate the server's field_serializer for pk/val
        for field in ("pk", "val"):
            if isinstance(entry.get(field), bytes):
                entry[field] = "b64:" + base64.b64encode(entry[field]).decode("ascii")
        pull_changes.append(entry)

    logger.info("  Pull response size: %d changes", len(pull_changes))

    # ── Client applies pull changes ──────────────────────────────────
    for ch in pull_changes:
        # Simulate client-side decodeCrrValue
        pk = ch["pk"]
        val = ch["val"]
        if isinstance(pk, str) and pk.startswith("b64:"):
            pk = base64.b64decode(pk[4:])
        if isinstance(val, str) and val.startswith("b64:"):
            val = base64.b64decode(val[4:])
        client.execute(
            """INSERT INTO crsql_changes ("table", pk, cid, val, col_version,
                                          db_version, site_id, cl, seq)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ch["table"], pk, ch["cid"], val, ch["col_version"],
             ch["db_version"], ch["site_id"], ch["cl"], ch["seq"]),
        )
    client.commit()

    # Verify client's local table has the merged data
    client_row = client.execute(
        "SELECT quantity, location FROM branch_inventory WHERE id = ?",
        (row_id,)
    ).fetchone()
    assert client_row is not None
    assert int(client_row[0]) == 75
    assert client_row[1] == "Roundtrip-Shelf"
    logger.info("  ✅ Client applied pull correctly: qty=%s location=%s", client_row[0], client_row[1])

    client.close()
    os.remove(client_path)

    logger.info("  ✅ Scenario 5 PASSED\n")


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _get_local_changes(conn: sqlite3.Connection, site_id: str) -> List[Tuple[Any, ...]]:
    """Read local crsql_changes rows."""
    cur = conn.execute(
        """SELECT "table", pk, cid, val, col_version, db_version,
                  site_id, cl, seq
           FROM crsql_changes
           ORDER BY seq"""
    )
    return [tuple(r) for r in cur.fetchall()]


def _insert_into_shadow(shadow: sqlite3.Connection, changes: List[Tuple[Any, ...]]) -> None:
    """Insert changes into shadow DB's crsql_changes (same as ShadowDB.insert_crr_changes)."""
    shadow.executemany(
        """INSERT INTO crsql_changes
           ("table", pk, cid, val, col_version, db_version, site_id, cl, seq)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        changes,
    )
    shadow.commit()


def _read_shadow_row(shadow: sqlite3.Connection, row_id: str) -> Optional[Dict[str, Any]]:
    shadow.row_factory = sqlite3.Row
    cur = shadow.execute("SELECT * FROM branch_inventory WHERE id = ?", (row_id,))
    row = cur.fetchone()
    shadow.row_factory = sqlite3.Row  # keep it
    return dict(row) if row else None


def _read_pg_row(pg: sqlite3.Connection, row_id: str) -> Optional[Dict[str, Any]]:
    """Read a row from the Postgres stand-in."""
    pg.row_factory = sqlite3.Row
    cur = pg.execute("SELECT * FROM branch_inventory WHERE id = ?", (row_id,))
    row = cur.fetchone()
    pg.row_factory = sqlite3.Row
    return dict(row) if row else None


def _upsert_to_pg(pg: sqlite3.Connection, row: Dict[str, Any],
                  shadow: Optional[sqlite3.Connection] = None) -> None:
    """Upsert a shadow merged row into the Postgres stand-in.

    Includes the duplicate business-key detection logic that mirrors
    the server-side ``_upsert_row_to_postgres`` in crr_sync_service.py.

    When *shadow* is provided and a duplicate is detected, the duplicate
    row is removed from the shadow DB (mirroring ``delete_crr_row``).
    """
    row = {k: v for k, v in row.items()
           if k not in ("rowid", "sync_status", "synced_at")}
    if not row:
        return

    new_id = row.get("id")
    branch_id = row.get("branch_id")
    drug_id = row.get("drug_id")

    # Duplicate business-key detection
    if branch_id and drug_id and new_id:
        existing = pg.execute(
            "SELECT * FROM branch_inventory "
            "WHERE branch_id = ? AND drug_id = ? AND id != ?",
            (branch_id, drug_id, new_id),
        ).fetchone()
        if existing is not None:
            pg.row_factory = sqlite3.Row
            existing_dict = dict(existing)

            # Merge
            merged = dict(existing_dict)
            merged["quantity"] = (
                int(existing_dict.get("quantity", 0) or 0)
                + int(row.get("quantity", 0) or 0)
            )
            merged["reserved_quantity"] = (
                int(existing_dict.get("reserved_quantity", 0) or 0)
                + int(row.get("reserved_quantity", 0) or 0)
            )
            existing_ts = str(existing_dict.get("updated_at") or "")
            incoming_ts = str(row.get("updated_at") or "")
            incoming_newer = incoming_ts > existing_ts
            if incoming_newer:
                merged["location"] = row.get("location") or existing_dict.get("location")
                merged["selling_price"] = row.get("selling_price") or existing_dict.get("selling_price")
            else:
                merged["location"] = existing_dict.get("location") or row.get("location")
                merged["selling_price"] = existing_dict.get("selling_price") or row.get("selling_price")
            merged["updated_at"] = max(existing_ts, incoming_ts)
            merged["created_at"] = min(
                str(existing_dict.get("created_at") or merged["updated_at"]),
                str(row.get("created_at") or merged["updated_at"]),
            )
            merged["sync_version"] = max(
                int(existing_dict.get("sync_version", 0) or 0),
                int(row.get("sync_version", 0) or 0),
            ) + 1

            set_clause = ", ".join(
                f"{c} = ?" for c in merged if c != "id"
            )
            values = [merged[c] for c in merged if c != "id"]
            values.append(merged["id"])
            pg.execute(
                f"UPDATE branch_inventory SET {set_clause} WHERE id = ?",
                values,
            )
            pg.commit()

            # Remove the duplicate from shadow DB (mirrors delete_crr_row)
            if shadow is not None:
                shadow.execute(
                    "DELETE FROM branch_inventory WHERE id = ?", (new_id,)
                )
                shadow.execute(
                    """DELETE FROM crsql_changes
                       WHERE "table" = 'branch_inventory' AND pk = ?""",
                    (new_id,),
                )
                shadow.commit()

            logger.info("  ⚡ Duplicate detected & merged via test helper")
            return

    # Normal upsert
    columns = [c for c in row.keys() if c != "rowid"]
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c != "id")
    pg.execute(
        f"""INSERT INTO branch_inventory ({", ".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT (id) DO UPDATE SET {updates}""",
        tuple(row[c] for c in columns),
    )
    pg.commit()


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

async def main():
    ext_path = _find_extension()
    if not ext_path:
        logger.error("cr-sqlite extension not found. Set CRSQLITE_EXTENSION_PATH.")
        sys.exit(1)
    logger.info("cr-sqlite extension: %s", ext_path)

    # Temp files
    shadow_path = "/tmp/e2e_shadow.db"
    pg_path = "/tmp/e2e_pg.db"
    for p in (shadow_path, pg_path):
        if os.path.exists(p):
            os.remove(p)

    try:
        # ── Set up shadow DB (with cr-sqlite) ────────────────────────────
        shadow = create_sqlite_db(shadow_path, SHADOW_SCHEMA, load_crsqlite=True)
        logger.info("Shadow DB ready at %s", shadow_path)

        # ── Set up Postgres stand-in (WITH uq_branch_drug constraint) ────
        pg = create_sqlite_db(pg_path, POSTGRES_STANDIN_SCHEMA, load_crsqlite=False)
        logger.info("Postgres stand-in ready at %s", pg_path)

        # ── Run scenarios ────────────────────────────────────────────────
        await test_blob_serialization()
        await test_field_level_merge(shadow, pg, ext_path)

        # Re-create shadow for fresh state
        shadow.close()
        pg.close()
        for p in (shadow_path, pg_path):
            if os.path.exists(p):
                os.remove(p)
        shadow = create_sqlite_db(shadow_path, SHADOW_SCHEMA, load_crsqlite=True)
        pg = create_sqlite_db(pg_path, POSTGRES_STANDIN_SCHEMA, load_crsqlite=False)

        await test_duplicate_business_key(shadow, pg)

        # Fresh state again for crash recovery
        shadow.close()
        pg.close()
        for p in (shadow_path, pg_path):
            if os.path.exists(p):
                os.remove(p)
        shadow = create_sqlite_db(shadow_path, SHADOW_SCHEMA, load_crsqlite=True)
        pg = create_sqlite_db(pg_path, POSTGRES_STANDIN_SCHEMA, load_crsqlite=False)

        await test_crash_recovery(shadow, pg)

        # Fresh state for full roundtrip
        shadow.close()
        pg.close()
        for p in (shadow_path, pg_path):
            if os.path.exists(p):
                os.remove(p)
        shadow = create_sqlite_db(shadow_path, SHADOW_SCHEMA, load_crsqlite=True)
        pg = create_sqlite_db(pg_path, POSTGRES_STANDIN_SCHEMA, load_crsqlite=False)

        await test_full_push_pull_roundtrip(shadow, pg, ext_path)

        logger.info("=" * 60)
        logger.info("  ALL SCENARIOS PASSED ✅")
        logger.info("=" * 60)

    finally:
        shadow.close()
        pg.close()
        for p in (shadow_path, pg_path):
            try:
                os.remove(p)
            except OSError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
