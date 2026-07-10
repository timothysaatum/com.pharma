"""
Shadow SQLite Database (CRDT peer)
===================================

Per ADR 0003, the server runs its own SQLite file with cr-sqlite loaded,
acting as one peer ("site") in the CRDT mesh.

Per ADR 0002, a single connection (Mutex-serialised) is used — no pooling.

Resolution order for the cr-sqlite extension path:
  1. CRSQLITE_EXTENSION_PATH env var
  2. crsqlite.so next to this module (app/services/sync/crsqlite.so)
  3. crsqlite.so in the working directory
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# ─── Per-table CRR configuration ────────────────────────────────────────
#
# Each entry declares:
#   ddl           — CREATE TABLE / index DDL for the shadow SQLite DB
#   strategy      — merge strategy for duplicate business-key collisions
#                     "sum_and_merge"        — sum numeric columns, newest-wins metadata
#                     "keep_both_renumber"   — keep both rows, renumber loser's business key
#                     "lww_with_external_dedup" — last-write-wins header, external dedup handles uniqueness
#   business_key  — list of column names that form the natural business key
#   sum_columns   — columns to sum (only for sum_and_merge)
#   bk_column     — the single column to renumber (only for keep_both_renumber)

_CRR_TABLE_CONFIG: Dict[str, Dict[str, Any]] = {
    "branch_inventory": {
        "ddl": """
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
        """,
        "strategy": "sum_and_merge",
        "business_key": ["branch_id", "drug_id"],
        "sum_columns": ["quantity", "reserved_quantity"],
    },
    "drug_batches": {
        "ddl": """
            CREATE TABLE IF NOT EXISTS drug_batches (
                id                  TEXT NOT NULL PRIMARY KEY,
                branch_id           TEXT NOT NULL DEFAULT '',
                drug_id             TEXT NOT NULL DEFAULT '',
                batch_number        TEXT NOT NULL DEFAULT '',
                quantity            INTEGER NOT NULL DEFAULT 0,
                remaining_quantity  INTEGER NOT NULL DEFAULT 0,
                manufacturing_date  TEXT,
                expiry_date         TEXT NOT NULL DEFAULT '',
                cost_price          REAL,
                selling_price       REAL,
                supplier            TEXT,
                purchase_order_id   TEXT,
                sync_status         TEXT NOT NULL DEFAULT 'synced',
                sync_version        INTEGER NOT NULL DEFAULT 1,
                synced_at           TEXT,
                updated_at          TEXT NOT NULL DEFAULT '',
                created_at          TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_drug_batches_lookup
                ON drug_batches(branch_id, drug_id, batch_number);
        """,
        "strategy": "sum_and_merge",
        "business_key": ["branch_id", "drug_id", "batch_number"],
        "sum_columns": ["quantity", "remaining_quantity"],
    },
    "customers": {
        "ddl": """
            CREATE TABLE IF NOT EXISTS customers (
                id                      TEXT NOT NULL PRIMARY KEY,
                organization_id         TEXT NOT NULL DEFAULT '',
                customer_type           TEXT NOT NULL DEFAULT 'walk_in',
                first_name              TEXT,
                last_name               TEXT,
                phone                   TEXT,
                email                   TEXT,
                date_of_birth           TEXT,
                loyalty_points          INTEGER NOT NULL DEFAULT 0,
                loyalty_tier            TEXT NOT NULL DEFAULT 'bronze',
                insurance_provider_id   TEXT,
                insurance_member_id     TEXT,
                preferred_contract_id   TEXT,
                is_active               INTEGER NOT NULL DEFAULT 1,
                is_deleted              INTEGER NOT NULL DEFAULT 0,
                sync_status             TEXT NOT NULL DEFAULT 'synced',
                sync_version            INTEGER NOT NULL DEFAULT 1,
                synced_at               TEXT,
                updated_at              TEXT NOT NULL DEFAULT '',
                created_at              TEXT NOT NULL DEFAULT ''
            );
        """,
        "strategy": "lww_with_external_dedup",
        "business_key": [],
        "match_any_columns": ["phone", "email"],
        "scope_columns": ["organization_id"],
        "additive_columns": ["loyalty_points", "total_orders", "total_value"],
    },
    "prescriptions": {
        "ddl": """
            CREATE TABLE IF NOT EXISTS prescriptions (
                id                    TEXT NOT NULL PRIMARY KEY,
                organization_id       TEXT NOT NULL DEFAULT '',
                branch_id             TEXT NOT NULL DEFAULT '',
                prescription_number   TEXT NOT NULL DEFAULT '',
                customer_id           TEXT NOT NULL DEFAULT '',
                prescriber_name       TEXT NOT NULL DEFAULT '',
                prescriber_license    TEXT NOT NULL DEFAULT '',
                prescriber_phone      TEXT,
                prescriber_address    TEXT,
                issue_date            TEXT NOT NULL DEFAULT '',
                expiry_date           TEXT NOT NULL DEFAULT '',
                medications           TEXT NOT NULL DEFAULT '[]',
                diagnosis             TEXT,
                notes                 TEXT,
                special_instructions  TEXT,
                refills_allowed       INTEGER NOT NULL DEFAULT 0,
                refills_remaining     INTEGER NOT NULL DEFAULT 0,
                last_refill_date      TEXT,
                status                TEXT NOT NULL DEFAULT 'active',
                verified_by           TEXT,
                verified_at           TEXT,
                sync_status           TEXT NOT NULL DEFAULT 'synced',
                sync_version          INTEGER NOT NULL DEFAULT 1,
                synced_at             TEXT,
                updated_at            TEXT NOT NULL DEFAULT '',
                created_at            TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_prescriptions_lookup
                ON prescriptions(organization_id, prescription_number);
        """,
        "strategy": "keep_both_renumber",
        "business_key": ["organization_id", "prescription_number"],
        "bk_column": "prescription_number",
    },
    "purchase_orders": {
        "ddl": """
            CREATE TABLE IF NOT EXISTS purchase_orders (
                id                    TEXT NOT NULL PRIMARY KEY,
                organization_id       TEXT NOT NULL DEFAULT '',
                branch_id             TEXT NOT NULL DEFAULT '',
                po_number             TEXT NOT NULL DEFAULT '',
                supplier_id           TEXT NOT NULL DEFAULT '',
                subtotal              REAL NOT NULL DEFAULT 0,
                tax_amount            REAL NOT NULL DEFAULT 0,
                shipping_cost         REAL NOT NULL DEFAULT 0,
                total_amount          REAL NOT NULL DEFAULT 0,
                status                TEXT NOT NULL DEFAULT 'draft',
                ordered_by            TEXT NOT NULL DEFAULT '',
                approved_by           TEXT,
                approved_at           TEXT,
                expected_delivery_date TEXT,
                received_date         TEXT,
                notes                 TEXT,
                items_json            TEXT NOT NULL DEFAULT '[]',
                sync_status           TEXT NOT NULL DEFAULT 'synced',
                sync_version          INTEGER NOT NULL DEFAULT 1,
                synced_at             TEXT,
                updated_at            TEXT NOT NULL DEFAULT '',
                created_at            TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_purchase_orders_lookup
                ON purchase_orders(branch_id, po_number);
        """,
        "strategy": "keep_both_renumber",
        "business_key": ["branch_id", "po_number"],
        "bk_column": "po_number",
    },
    "sales": {
        "ddl": """
            CREATE TABLE IF NOT EXISTS sales (
                id                            TEXT NOT NULL PRIMARY KEY,
                organization_id               TEXT NOT NULL DEFAULT '',
                branch_id                     TEXT NOT NULL DEFAULT '',
                sale_number                   TEXT NOT NULL DEFAULT '',
                customer_id                   TEXT,
                customer_name                 TEXT,
                subtotal                      REAL NOT NULL DEFAULT 0,
                discount_amount               REAL NOT NULL DEFAULT 0,
                tax_amount                    REAL NOT NULL DEFAULT 0,
                total_amount                  REAL NOT NULL DEFAULT 0,
                price_contract_id             TEXT,
                contract_name                 TEXT,
                contract_discount_percentage  REAL,
                contract_type                 TEXT,
                payment_method                TEXT NOT NULL DEFAULT 'cash',
                payment_status                TEXT NOT NULL DEFAULT 'completed',
                amount_paid                   REAL,
                change_amount                 REAL NOT NULL DEFAULT 0,
                payment_reference             TEXT,
                split_payment_details         TEXT,
                insurance_preauth_number      TEXT,
                prescription_id               TEXT,
                prescription_number           TEXT,
                prescriber_name               TEXT,
                prescriber_license            TEXT,
                cashier_id                    TEXT NOT NULL DEFAULT '',
                pharmacist_id                 TEXT,
                insurance_claim_number        TEXT,
                patient_copay_amount          REAL,
                insurance_covered_amount      REAL,
                insurance_verified            INTEGER NOT NULL DEFAULT 0,
                insurance_verified_at         TEXT,
                insurance_verified_by         TEXT,
                notes                         TEXT,
                status                        TEXT NOT NULL DEFAULT 'completed',
                cancelled_at                  TEXT,
                cancelled_by                  TEXT,
                cancellation_reason           TEXT,
                refund_amount                 REAL,
                refunded_at                   TEXT,
                refunded_by                   TEXT,
                refund_reason                 TEXT,
                refund_reference              TEXT,
                receipt_printed               INTEGER NOT NULL DEFAULT 0,
                receipt_emailed               INTEGER NOT NULL DEFAULT 0,
                items_json                    TEXT NOT NULL DEFAULT '[]',
                items_count                   INTEGER NOT NULL DEFAULT 0,
                sync_status                   TEXT NOT NULL DEFAULT 'synced',
                sync_version                  INTEGER NOT NULL DEFAULT 1,
                synced_at                     TEXT,
                updated_at                    TEXT NOT NULL DEFAULT '',
                created_at                    TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_sales_lookup
                ON sales(branch_id, sale_number);
        """,
        "strategy": "keep_both_renumber",
        "business_key": ["branch_id", "sale_number"],
        "bk_column": "sale_number",
    },
}


def get_crr_table_names() -> List[str]:
    return list(_CRR_TABLE_CONFIG.keys())


def get_crr_config(table: str) -> Optional[Dict[str, Any]]:
    return _CRR_TABLE_CONFIG.get(table)


# ─── Extension resolution (same logic as client db.rs) ────────────────

def _resolve_extension_path() -> Optional[str]:
    if val := os.environ.get("CRSQLITE_EXTENSION_PATH"):
        if os.path.exists(val):
            return val

    candidates = [
        Path(__file__).parent / "crsqlite.so",
        Path("crsqlite.so"),
    ]
    for p in candidates:
        if p.exists():
            return str(p.resolve())
    return None


# ─── Business key / disambiguation helpers ─────────────────────────────

_BK_SUFFIX_PATTERN = re.compile(r"-[A-Z]$")


def _disambiguate_business_key(bk: str, existing_keys: List[str]) -> str:
    """Append a suffix like ``-B``, ``-C``, etc. to avoid collision.

    If ``bk`` already ends with ``-X`` (single letter), it is stripped first
    so that ``SALE-1042-B`` → ``SALE-1042-C`` on a subsequent collision.
    """
    base = bk
    if _BK_SUFFIX_PATTERN.search(bk):
        base = bk[:-2]

    used = set(existing_keys)
    for suffix_letter in "BCDEFGHIJKLMNOPQRSTUVWXYZ":
        candidate = f"{base}-{suffix_letter}"
        if candidate not in used:
            return candidate
    raise RuntimeError(f"Could not disambiguate business key {bk} (26+ collisions)")


# ─── Audit log for renumbering events ─────────────────────────────────

_AUDIT_LOG_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS crr_renumber_audit (
        id                SERIAL PRIMARY KEY,
        event_id          VARCHAR(512) NOT NULL UNIQUE,
        table_name        VARCHAR(100) NOT NULL,
        winner_id         VARCHAR(36) NOT NULL,
        loser_id          VARCHAR(36) NOT NULL,
        business_key_col  VARCHAR(100) NOT NULL,
        old_business_key  VARCHAR(255) NOT NULL,
        new_business_key  VARCHAR(255) NOT NULL,
        renumbered_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
    );
"""

_CUSTOMER_MERGE_AUDIT_DDL = """
    CREATE TABLE IF NOT EXISTS crr_customer_merge_audit (
        id                    SERIAL PRIMARY KEY,
        event_id              VARCHAR(255) NOT NULL UNIQUE,
        organization_id       VARCHAR(36) NOT NULL,
        survivor_id           VARCHAR(36) NOT NULL,
        loser_id              VARCHAR(36) NOT NULL,
        matched_fields        JSONB NOT NULL,
        field_resolutions     JSONB NOT NULL,
        merged_at             TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
    );
"""


async def _ensure_audit_table(db: AsyncSession) -> None:
    """Create the ``crr_renumber_audit`` table if it does not exist."""
    await db.execute(text(_AUDIT_LOG_TABLE_DDL))


async def _log_renumbering(
    db: AsyncSession,
    table: str,
    winner_id: str,
    loser_id: str,
    bk_col: str,
    old_bk: str,
    new_bk: str,
) -> None:
    event_id = f"{table}:{loser_id}:{old_bk}:{new_bk}"
    await db.execute(
        text("""
            INSERT INTO crr_renumber_audit
                (event_id, table_name, winner_id, loser_id, business_key_col,
                 old_business_key, new_business_key)
            VALUES
                (:event_id, :table_name, :winner_id, :loser_id, :business_key_col,
                 :old_business_key, :new_business_key)
            ON CONFLICT (event_id) DO NOTHING
        """),
        {
            "event_id": event_id,
            "table_name": table,
            "winner_id": winner_id,
            "loser_id": loser_id,
            "business_key_col": bk_col,
            "old_business_key": old_bk,
            "new_business_key": new_bk,
        },
    )


# ─── Shadow DB singleton ──────────────────────────────────────────────

class ShadowDB:
    """Server-side shadow SQLite database with cr-sqlite loaded.

    Follows ADR 0002's single-connection model: a mutex guards all access.
    All I/O runs via ``asyncio.to_thread`` so it does not block the event loop.
    """

    def __init__(self) -> None:
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = Lock()
        self._initialized = False
        self._db_path: str = ""
        self._ext_path: Optional[str] = None

    # ── Initialisation ────────────────────────────────────────────────

    async def initialize(
        self,
        db_path: str = "",
        ext_path: Optional[str] = None,
    ) -> None:
        if self._initialized:
            return
        self._db_path = db_path or os.environ.get(
            "SHADOW_DB_PATH", "shadow.db"
        )
        self._ext_path = ext_path or _resolve_extension_path()
        await asyncio.to_thread(self._init_sync)
        self._initialized = True
        logger.info(
            "Shadow DB initialised at %s (cr-sqlite: %s)",
            self._db_path,
            self._ext_path or "not loaded",
        )

    def _init_sync(self) -> None:
        db_path = self._db_path
        logger.info("Opening shadow SQLite database: %s", db_path)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")

        if self._ext_path:
            try:
                conn.enable_load_extension(True)
                conn.load_extension(self._ext_path)
                logger.info("cr-sqlite extension loaded from %s", self._ext_path)
            except Exception as exc:
                logger.warning(
                    "Failed to load cr-sqlite extension: %s — "
                    "running without CRDT support",
                    exc,
                )
                self._ext_path = None
        else:
            logger.warning(
                "cr-sqlite extension not found — running without CRDT support"
            )

        # Create CRR table schemas
        for table_name, cfg in _CRR_TABLE_CONFIG.items():
            conn.executescript(cfg["ddl"])
            if self._ext_path:
                try:
                    conn.execute(f"SELECT crsql_as_crr('{table_name}')")
                    logger.info("Table %s converted to CRR", table_name)
                except Exception as exc:
                    logger.warning(
                        "crsql_as_crr for %s failed: %s", table_name, exc
                    )
        conn.commit()
        self._conn = conn

    # ── CRR push: insert remote changes, trigger merge ────────────────

    async def insert_crr_changes(
        self, changes: List[Tuple[Any, ...]]
    ) -> None:
        """Insert raw crsql_changes rows into the shadow DB.

        Each tuple must match the crsql_changes column order:
          table, pk, cid, val, col_version, db_version, site_id, cl, seq

        cr-sqlite triggers fire on INSERT into crsql_changes, merging
        the change into the tracked CRR table automatically.
        """
        if not changes:
            return

        def _sync() -> None:
            with self._lock:
                conn = self._conn
                if conn is None:
                    raise RuntimeError("Shadow DB not initialised")
                conn.executemany(
                    """INSERT INTO crsql_changes
                       ("table", pk, cid, val, col_version, db_version,
                        site_id, cl, seq)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    changes,
                )
                conn.commit()

        await asyncio.to_thread(_sync)

    # ── Read merged state from shadow table ───────────────────────────

    async def get_merged_row(
        self, table: str, row_id: str
    ) -> Optional[Dict[str, Any]]:
        """Return the current merged state of a single row from the shadow DB."""

        def _sync() -> Optional[Dict[str, Any]]:
            with self._lock:
                conn = self._conn
                if conn is None:
                    raise RuntimeError("Shadow DB not initialised")
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    f"SELECT * FROM [{table}] WHERE id = ?", (row_id,)
                )
                row = cur.fetchone()
                return dict(row) if row else None

        return await asyncio.to_thread(_sync)

    # ── List all rows that changed in crsql_changes (for pull) ────────

    async def get_changes_since(
        self, since_db_version: int = 0, limit: int = 500
    ) -> List[Dict[str, Any]]:
        """Return crsql_changes rows with db_version > since_db_version.

        This is what the client pulls to apply server-side changes locally.
        """
        def _sync() -> List[Dict[str, Any]]:
            with self._lock:
                conn = self._conn
                if conn is None:
                    raise RuntimeError("Shadow DB not initialised")
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    """SELECT "table", pk, cid, val, col_version, db_version,
                             site_id, cl, seq
                      FROM crsql_changes
                      WHERE db_version > ?
                      ORDER BY seq
                      LIMIT ?""",
                    (since_db_version, limit),
                )
                return [dict(r) for r in cur.fetchall()]

        return await asyncio.to_thread(_sync)

    # ── Get max db_version (for pull pagination) ──────────────────────

    async def max_db_version(self) -> int:
        def _sync() -> int:
            with self._lock:
                conn = self._conn
                if conn is None:
                    return 0
                cur = conn.execute(
                    "SELECT COALESCE(MAX(db_version), 0) FROM crsql_changes"
                )
                return cur.fetchone()[0] or 0

        return await asyncio.to_thread(_sync)

    # ── Upsert a single merged row from shadow into Postgres ──────────

    async def get_merged_row_for_upsert(
        self, table: str, row_id: str
    ) -> Optional[Dict[str, Any]]:
        """Fetch a merged row from the shadow DB, stripped of sync-metadata.

        The caller (push handler) then upserts this into Postgres using the
        appropriate ORM model and existing FK/field validation logic.
        """
        merged = await self.get_merged_row(table, row_id)
        if merged is None:
            return None
        merged.pop("sync_status", None)
        merged.pop("synced_at", None)
        merged.pop("rowid", None)
        return merged

    # ── Delete a duplicate row from shadow DB (business-key collision) ─

    async def delete_crr_row(self, table: str, row_id: str) -> None:
        """Delete a row and retain cr-sqlite's generated tombstone history.

        Used by ``sum_and_merge`` and ``lww_with_external_dedup`` strategies
        when a duplicate is merged into the winner. ``pk`` in
        ``crsql_changes`` is cr-sqlite encoded, so comparing it to the textual
        application ID is incorrect. More importantly, deleting that history
        would suppress the tombstone clients need to remove their local loser.
        """
        def _sync() -> None:
            with self._lock:
                conn = self._conn
                if conn is None:
                    raise RuntimeError("Shadow DB not initialised")
                conn.execute(
                    f"DELETE FROM [{table}] WHERE id = ?", (row_id,)
                )
                conn.commit()

        await asyncio.to_thread(_sync)

    # ── Update business key in shadow for renumbered rows ─────────────

    async def update_crr_row_business_key(
        self, table: str, row_id: str, bk_column: str, new_bk_value: str
    ) -> None:
        """Update the business-key column of a row in the shadow DB.

        Used by ``keep_both_renumber`` to persist the disambiguated key
        so subsequent pulls reflect the corrected value.
        """
        def _sync() -> None:
            with self._lock:
                conn = self._conn
                if conn is None:
                    raise RuntimeError("Shadow DB not initialised")
                conn.execute(
                    f"UPDATE [{table}] SET [{bk_column}] = ? WHERE id = ?",
                    (new_bk_value, row_id),
                )
                conn.commit()

        await asyncio.to_thread(_sync)

    # ── Postgres upsert with per-table merge strategy ─────────────────

    async def upsert_merged_row(
        self,
        db: AsyncSession,
        table: str,
        row: Dict[str, Any],
    ) -> None:
        """Upsert a merged shadow-DB row into Postgres.

        Includes duplicate business-key detection and resolution
        using the per-table strategy declared in ``_CRR_TABLE_CONFIG``.
        """
        row = {k: v for k, v in row.items()
               if k not in ("rowid", "sync_status", "synced_at")}
        if not row:
            return
        if table == "customers":
            row = self._coerce_customer_pg_types(row)

        cfg = _CRR_TABLE_CONFIG.get(table)
        if cfg is None:
            logger.warning("No CRR config for table %s — skipping upsert", table)
            return

        strategy = cfg["strategy"]
        bk_columns = cfg["business_key"]

        # Customer identity is an OR-match across non-empty contact fields,
        # unlike the composite AND business keys used by the other strategies.
        if strategy == "lww_with_external_dedup":
            duplicates = await self._find_external_duplicates(
                db, table, row, cfg
            )
            if duplicates:
                await self._handle_lww_with_external_dedup(
                    db, table, duplicates, row, cfg
                )
                return

        # ── Duplicate business-key detection ──────────────────────────
        if bk_columns:
            bk_values = {col: row.get(col) for col in bk_columns}
            if all(bk_values.values()):
                conditions = " AND ".join(
                    f"{col} = :{col}" for col in bk_columns
                )
                existing = await db.execute(
                    text(f"""
                        SELECT * FROM {table}
                        WHERE {conditions}
                          AND id != :new_id
                        LIMIT 1
                    """),
                    {**bk_values, "new_id": row.get("id")},
                )
                existing_row = existing.mappings().first()
                if existing_row is not None:
                    existing_row = dict(existing_row)

                    if strategy == "sum_and_merge":
                        await self._handle_sum_and_merge(
                            db, table, existing_row, row, cfg
                        )
                        return

                    elif strategy == "keep_both_renumber":
                        await self._handle_keep_both_renumber(
                            db, table, existing_row, row, cfg
                        )
                        return

        # ── Normal upsert (no collision, or no business key) ──────────
        if table == "customers":
            row = self._coerce_customer_pg_types(row, include_defaults=True)
        columns = list(row.keys())
        placeholders = [f":{c}" for c in columns]
        updates = [f"{c} = EXCLUDED.{c}" for c in columns if c != "id"]

        stmt = text(f"""
            INSERT INTO {table} ({', '.join(columns)})
            VALUES ({', '.join(placeholders)})
            ON CONFLICT (id) DO UPDATE SET
                {', '.join(updates)}
        """)
        await db.execute(stmt, row)

    # ── Strategy handlers ─────────────────────────────────────────────

    @staticmethod
    def _coerce_customer_pg_types(
        row: Dict[str, Any], include_defaults: bool = False
    ) -> Dict[str, Any]:
        coerced = dict(row)
        server_defaults = {
            "address": None,
            # ARRAY is represented by the project's portable DB type as JSON
            # text on this Postgres schema when using raw SQL.
            "allergies": "[]",
            "chronic_conditions": "[]",
            "medical_data_encrypted": False,
            "total_orders": 0,
            "total_value": 0.0,
            "preferred_contact_method": "email",
            "marketing_consent": False,
            "insurance_card_image_url": None,
            "sync_status": "synced",
        }
        if include_defaults:
            for column, default in server_defaults.items():
                coerced.setdefault(column, default)
        for column in ("is_active", "is_deleted", "marketing_consent", "medical_data_encrypted"):
            if column in coerced and coerced[column] is not None:
                coerced[column] = bool(coerced[column])
        for column in ("created_at", "updated_at"):
            value = coerced.get(column)
            if isinstance(value, str) and value:
                coerced[column] = datetime.fromisoformat(value.replace("Z", "+00:00"))
        value = coerced.get("date_of_birth")
        if isinstance(value, str) and value:
            coerced["date_of_birth"] = date.fromisoformat(value)
        return coerced

    async def _handle_sum_and_merge(
        self,
        db: AsyncSession,
        table: str,
        existing: Dict[str, Any],
        incoming: Dict[str, Any],
        cfg: Dict[str, Any],
    ) -> None:
        """Sum numeric columns, newest-wins metadata. Delete the loser from shadow."""
        merged = dict(existing)
        sum_cols = set(cfg.get("sum_columns", []))
        incoming_newer = self._incoming_newer(existing, incoming)

        for k, v in incoming.items():
            if k == "id":
                continue
            if k in sum_cols:
                merged[k] = (
                    int(existing.get(k, 0) or 0) + int(v or 0)
                )
            elif incoming_newer:
                merged[k] = v if v is not None else existing.get(k)
            else:
                merged[k] = existing.get(k) if existing.get(k) is not None else v

        merged["updated_at"] = max(
            str(existing.get("updated_at") or ""),
            str(incoming.get("updated_at") or ""),
        )
        merged["created_at"] = min(
            str(existing.get("created_at") or merged["updated_at"]),
            str(incoming.get("created_at") or merged["updated_at"]),
        )
        merged["sync_version"] = max(
            int(existing.get("sync_version", 0) or 0),
            int(incoming.get("sync_version", 0) or 0),
        ) + 1
        merged.pop("rowid", None)
        merged.pop("synced_at", None)

        set_clause = ", ".join(
            f"{c} = :{c}" for c in merged if c != "id"
        )
        await db.execute(
            text(f"UPDATE {table} SET {set_clause} WHERE id = :id"),
            merged,
        )
        await self.delete_crr_row(table, str(incoming.get("id")))

    async def _handle_keep_both_renumber(
        self,
        db: AsyncSession,
        table: str,
        existing: Dict[str, Any],
        incoming: Dict[str, Any],
        cfg: Dict[str, Any],
    ) -> None:
        """Keep both rows. Renumber the loser's business key with a suffix.

        Winner = the row already in Postgres (``existing``).
        Loser = incoming row (gets -B, -C, … suffix).
        """
        bk_column = cfg.get("bk_column", "")
        if not bk_column:
            logger.warning(
                "keep_both_renumber strategy for %s has no bk_column — "
                "falling through to normal upsert",
                table,
            )
            return

        loser_id = str(incoming.get("id"))
        old_bk = str(incoming.get(bk_column, ""))
        winner_id = str(existing.get("id"))

        # Collect existing business keys to avoid collision
        all_bk_rows = await db.execute(
            text(f"""
                SELECT DISTINCT [{bk_column}] FROM {table}
                WHERE [{bk_column}] LIKE :pattern
            """),
            {"pattern": f"{old_bk}%"},
        )
        existing_keys = [row[0] for row in all_bk_rows.fetchall() if row[0]]
        new_bk = _disambiguate_business_key(old_bk, existing_keys)

        # ── 1. Update incoming row's business key in Postgres ─────────
        incoming_row = dict(incoming)
        incoming_row[bk_column] = new_bk
        incoming_row.pop("rowid", None)
        incoming_row.pop("synced_at", None)

        columns = list(incoming_row.keys())
        placeholders = [f":{c}" for c in columns]
        updates = [f"{c} = EXCLUDED.{c}" for c in columns if c != "id"]
        await db.execute(
            text(f"""
                INSERT INTO {table} ({', '.join(columns)})
                VALUES ({', '.join(placeholders)})
                ON CONFLICT (id) DO UPDATE SET
                    {', '.join(updates)}
            """),
            incoming_row,
        )

        # ── 2. Update loser's business key in shadow DB ───────────────
        await self.update_crr_row_business_key(table, loser_id, bk_column, new_bk)

        # ── 3. Log renumbering event ──────────────────────────────────
        await _ensure_audit_table(db)
        await _log_renumbering(
            db, table, winner_id, loser_id,
            bk_column, old_bk, new_bk,
        )

    async def _find_external_duplicates(
        self,
        db: AsyncSession,
        table: str,
        incoming: Dict[str, Any],
        cfg: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Find rows matching any configured non-empty contact field."""
        match_values: Dict[str, str] = {}
        for column in cfg.get("match_any_columns", []):
            value = str(incoming.get(column) or "").strip()
            if value:
                match_values[column] = value
        if not match_values:
            return []

        scope_values = {
            column: incoming.get(column)
            for column in cfg.get("scope_columns", [])
        }
        if not scope_values or not all(scope_values.values()):
            return []

        scope_sql = " AND ".join(
            f"{column} = :scope_{column}" for column in scope_values
        )
        match_sql = " OR ".join(
            f"TRIM({column}) = :match_{column}" for column in match_values
        )
        params = {
            **{f"scope_{key}": value for key, value in scope_values.items()},
            **{f"match_{key}": value for key, value in match_values.items()},
            "incoming_id": incoming.get("id"),
        }
        result = await db.execute(text(f"""
            SELECT * FROM {table}
            WHERE {scope_sql}
              AND id != :incoming_id
              AND ({match_sql})
            ORDER BY created_at, id
        """), params)
        return [dict(row) for row in result.mappings().all()]

    async def _handle_lww_with_external_dedup(
        self,
        db: AsyncSession,
        table: str,
        existing_rows: List[Dict[str, Any]],
        incoming: Dict[str, Any],
        cfg: Dict[str, Any],
    ) -> None:
        """Run the complete customer merge in a Postgres savepoint."""
        async with db.begin_nested():
            await self._merge_customer_duplicates(
                db, table, existing_rows, incoming, cfg
            )

    async def _merge_customer_duplicates(
        self,
        db: AsyncSession,
        table: str,
        existing_rows: List[Dict[str, Any]],
        incoming: Dict[str, Any],
        cfg: Dict[str, Any],
    ) -> None:
        """Merge duplicate customer identities without losing data or references."""
        candidates = [*existing_rows, dict(incoming)]
        candidates.sort(key=lambda row: (
            str(row.get("created_at") or "9999-12-31"),
            str(row.get("id") or ""),
        ))
        survivor = candidates[0]
        losers = candidates[1:]
        survivor_id = str(survivor["id"])
        loser_ids = [str(row["id"]) for row in losers]
        additive = set(cfg.get("additive_columns", []))
        metadata = {
            "id", "rowid", "synced_at", "last_synced_at", "sync_hash",
            "created_at", "updated_at", "sync_version", "sync_status",
        }

        merged = dict(survivor)
        field_resolutions: List[Dict[str, Any]] = []
        all_columns = set().union(*(row.keys() for row in candidates))
        for column in sorted(all_columns - metadata - {"organization_id"}):
            values = [row.get(column) for row in candidates]
            present = [value for value in values if value is not None and value != ""]
            if column in additive:
                merged[column] = sum(present) if present else 0
                if len(set(map(str, present))) > 1 or len(present) > 1:
                    field_resolutions.append({
                        "field": column,
                        "values": values,
                        "resolution": "sum",
                        "result": merged[column],
                    })
                continue
            if not present:
                merged[column] = survivor.get(column)
                continue

            newest_with_value = max(
                (row for row in candidates
                 if row.get(column) is not None and row.get(column) != ""),
                key=lambda row: (
                    str(row.get("updated_at") or ""), str(row.get("id") or "")
                ),
            )
            merged[column] = newest_with_value.get(column)
            distinct = {str(value) for value in present}
            if len(distinct) > 1:
                field_resolutions.append({
                    "field": column,
                    "values": values,
                    "resolution": "newest_non_null",
                    "source_id": str(newest_with_value.get("id")),
                    "result": merged[column],
                })

        merged["id"] = survivor_id
        merged["organization_id"] = survivor.get("organization_id")
        created_source = min(
            (row for row in candidates if row.get("created_at")),
            key=lambda row: str(row.get("created_at")),
        )
        updated_source = max(
            (row for row in candidates if row.get("updated_at")),
            key=lambda row: str(row.get("updated_at")),
        )
        merged["created_at"] = created_source.get("created_at")
        merged["updated_at"] = updated_source.get("updated_at")
        merged["sync_version"] = max(
            int(row.get("sync_version", 0) or 0) for row in candidates
        ) + 1
        merged.pop("rowid", None)
        merged.pop("synced_at", None)
        merged.pop("last_synced_at", None)
        merged.pop("sync_hash", None)
        merged = self._coerce_customer_pg_types(merged)

        # Upsert the survivor first so an incoming-earliest customer exists
        # before dependent references are repointed.
        columns = list(merged.keys())
        updates = [f"{column} = EXCLUDED.{column}" for column in columns if column != "id"]
        await db.execute(text(f"""
            INSERT INTO customers ({', '.join(columns)})
            VALUES ({', '.join(f':{column}' for column in columns)})
            ON CONFLICT (id) DO UPDATE SET {', '.join(updates)}
        """), merged)

        for loser_id in loser_ids:
            await db.execute(text(
                "UPDATE sales SET customer_id = :survivor WHERE customer_id = :loser"
            ), {"survivor": survivor_id, "loser": loser_id})
            await db.execute(text(
                "UPDATE prescriptions SET customer_id = :survivor WHERE customer_id = :loser"
            ), {"survivor": survivor_id, "loser": loser_id})

        await db.execute(text(_CUSTOMER_MERGE_AUDIT_DDL))
        for loser in losers:
            matched_fields = []
            for column in cfg.get("match_any_columns", []):
                loser_value = str(loser.get(column) or "").strip()
                matched_another = any(
                    str(candidate.get(column) or "").strip() == loser_value
                    for candidate in candidates
                    if str(candidate.get("id")) != str(loser.get("id"))
                )
                if loser_value and matched_another:
                    matched_fields.append({"field": column, "value": loser_value})
            event_id = f"customers:{survivor_id}:{loser['id']}"
            await db.execute(text("""
                INSERT INTO crr_customer_merge_audit
                    (event_id, organization_id, survivor_id, loser_id,
                     matched_fields, field_resolutions)
                VALUES
                    (:event_id, :organization_id, :survivor_id, :loser_id,
                     CAST(:matched_fields AS JSONB), CAST(:field_resolutions AS JSONB))
                ON CONFLICT (event_id) DO NOTHING
            """), {
                "event_id": event_id,
                "organization_id": str(merged.get("organization_id")),
                "survivor_id": survivor_id,
                "loser_id": str(loser["id"]),
                "matched_fields": json.dumps(matched_fields, default=str),
                "field_resolutions": json.dumps(field_resolutions, default=str),
            })

        if loser_ids:
            await db.execute(
                text("DELETE FROM customers WHERE id IN :loser_ids").bindparams(
                    bindparam("loser_ids", expanding=True)
                ),
                {"loser_ids": loser_ids},
            )

        # Shadow cleanup happens only after every Postgres mutation above has
        # succeeded. Reconciliation can safely retry the idempotent SQL work.
        for loser_id in loser_ids:
            await self.delete_crr_row(table, loser_id)

    @staticmethod
    def _incoming_newer(existing: Dict[str, Any], incoming: Dict[str, Any]) -> bool:
        existing_ts = str(existing.get("updated_at") or "")
        incoming_ts = str(incoming.get("updated_at") or "")
        return incoming_ts > existing_ts

    # ── Reconciliation (crash recovery + periodic) ──────────────────

    async def reconcile_table(
        self,
        table: str,
        db: AsyncSession,
    ) -> Tuple[int, int]:
        """Read ALL rows from shadow table and upsert into Postgres.

        Returns (checked, updated) counts.
        Used after startup or crash recovery to re-sync the two stores.
        """
        def _fetch_all() -> List[Dict[str, Any]]:
            with self._lock:
                conn = self._conn
                if conn is None:
                    return []
                conn.row_factory = sqlite3.Row
                cur = conn.execute(f"SELECT * FROM [{table}]")
                return [dict(r) for r in cur.fetchall()]

        rows = await asyncio.to_thread(_fetch_all)
        checked = len(rows)
        updated = 0
        for row in rows:
            row_id = row.get("id")
            if row_id:
                merged = await self.get_merged_row(table, row_id)
                if merged is not None:
                    await self.upsert_merged_row(db, table, merged)
                    updated += 1
        return checked, updated


# ── Module-level singleton ────────────────────────────────────────────

_shadow_db = ShadowDB()


async def get_shadow_db() -> ShadowDB:
    """Return the initialised shadow DB singleton."""
    if not _shadow_db._initialized:
        await _shadow_db.initialize()
    return _shadow_db
