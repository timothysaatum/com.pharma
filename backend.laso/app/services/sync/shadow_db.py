"""
Shadow SQLite Database (CRDT peer)
===================================

Per ADR 0003, the server runs its own SQLite file with cr-sqlite loaded,
acting as one peer ("site") in the CRDT mesh.

Per ADR 0002, a single connection (Mutex-serialised) is used — no pooling.

Resolution order for the cr-sqlite extension path:
  1. CRSQLITE_EXTENSION_PATH env var
  2. Explicit monorepo layout (crsqlite/linux-<arch>/crsqlite.so)
  3. Explicit packaged/current-working-directory layouts
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import platform
import re
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, Numeric, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.base import Base
# Register every mapped table before type coercion inspects Base.metadata.
# ShadowDB is also used by standalone reconciliation/e2e processes that do not
# import FastAPI's normal model-loading path first.
import app.models as _registered_models  # noqa: F401, E402

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
    "drugs": {
        "ddl": """
            CREATE TABLE IF NOT EXISTS drugs (
                id                TEXT NOT NULL PRIMARY KEY,
                organization_id   TEXT NOT NULL DEFAULT '',
                name              TEXT NOT NULL DEFAULT '',
                generic_name      TEXT,
                brand_name        TEXT,
                sku               TEXT,
                barcode           TEXT,
                category_id       TEXT,
                drug_type         TEXT NOT NULL DEFAULT 'otc',
                dosage_form       TEXT,
                strength          TEXT,
                manufacturer      TEXT,
                supplier          TEXT,
                requires_prescription INTEGER NOT NULL DEFAULT 0,
                controlled_substance_schedule TEXT,
                ndc_code          TEXT,
                unit_price        REAL NOT NULL DEFAULT 0,
                cost_price        REAL,
                markup_percentage REAL,
                tax_rate          REAL NOT NULL DEFAULT 0,
                reorder_level     INTEGER NOT NULL DEFAULT 10,
                reorder_quantity  INTEGER NOT NULL DEFAULT 50,
                max_stock_level   INTEGER,
                unit_of_measure   TEXT NOT NULL DEFAULT 'unit',
                description       TEXT,
                usage_instructions TEXT,
                side_effects      TEXT,
                contraindications TEXT,
                storage_conditions TEXT,
                image_url         TEXT,
                is_active         INTEGER NOT NULL DEFAULT 1,
                is_deleted        INTEGER NOT NULL DEFAULT 0,
                sync_status       TEXT NOT NULL DEFAULT 'synced',
                sync_version      INTEGER NOT NULL DEFAULT 1,
                synced_at         TEXT,
                updated_at        TEXT NOT NULL DEFAULT '',
                created_at        TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_drugs_org ON drugs(organization_id);
        """,
        "strategy": "lww",
        "business_key": [],
        "server_authoritative": True,
    },
    "drug_categories": {
        "ddl": """
            CREATE TABLE IF NOT EXISTS drug_categories (
                id              TEXT NOT NULL PRIMARY KEY,
                organization_id TEXT NOT NULL DEFAULT '',
                name            TEXT NOT NULL DEFAULT '',
                description     TEXT,
                parent_id       TEXT,
                path            TEXT,
                level           INTEGER NOT NULL DEFAULT 0,
                is_deleted      INTEGER NOT NULL DEFAULT 0,
                sync_status     TEXT NOT NULL DEFAULT 'synced',
                sync_version    INTEGER NOT NULL DEFAULT 1,
                synced_at       TEXT,
                updated_at      TEXT NOT NULL DEFAULT '',
                created_at      TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_drug_categories_org ON drug_categories(organization_id);
        """,
        "strategy": "lww",
        "business_key": [],
        "server_authoritative": True,
    },
    "price_contracts": {
        "ddl": """
            CREATE TABLE IF NOT EXISTS price_contracts (
                id                        TEXT NOT NULL PRIMARY KEY,
                organization_id           TEXT NOT NULL DEFAULT '',
                contract_code             TEXT NOT NULL DEFAULT '',
                contract_name             TEXT NOT NULL DEFAULT '',
                contract_type             TEXT NOT NULL DEFAULT 'standard',
                is_default_contract       INTEGER NOT NULL DEFAULT 0,
                discount_type             TEXT NOT NULL DEFAULT 'percentage',
                discount_percentage       REAL NOT NULL DEFAULT 0,
                applies_to_prescription_only INTEGER NOT NULL DEFAULT 0,
                applies_to_otc            INTEGER NOT NULL DEFAULT 1,
                applies_to_all_branches   INTEGER NOT NULL DEFAULT 1,
                applicable_branch_ids     TEXT NOT NULL DEFAULT '[]',
                effective_from            TEXT NOT NULL DEFAULT '',
                effective_to              TEXT,
                requires_verification     INTEGER NOT NULL DEFAULT 0,
                requires_approval         INTEGER NOT NULL DEFAULT 0,
                daily_usage_limit         INTEGER,
                per_customer_usage_limit  INTEGER,
                insurance_provider_id     TEXT,
                requires_preauthorization INTEGER NOT NULL DEFAULT 0,
                minimum_purchase_amount   REAL,
                maximum_purchase_amount   REAL,
                status                    TEXT NOT NULL DEFAULT 'active',
                is_active                 INTEGER NOT NULL DEFAULT 1,
                copay_amount              REAL,
                copay_percentage          REAL,
                is_deleted                INTEGER NOT NULL DEFAULT 0,
                sync_status               TEXT NOT NULL DEFAULT 'synced',
                sync_version              INTEGER NOT NULL DEFAULT 1,
                synced_at                 TEXT,
                updated_at                TEXT NOT NULL DEFAULT '',
                created_at                TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_price_contracts_org ON price_contracts(organization_id);
        """,
        "strategy": "lww",
        "business_key": [],
        "server_authoritative": True,
    },
    "audit_logs": {
        "ddl": """
            CREATE TABLE IF NOT EXISTS audit_logs (
                id                TEXT NOT NULL PRIMARY KEY,
                organization_id   TEXT NOT NULL DEFAULT '',
                user_id           TEXT,
                user_full_name    TEXT,
                action            TEXT NOT NULL DEFAULT '',
                entity_type       TEXT,
                entity_id         TEXT,
                changes           TEXT,
                ip_address        TEXT,
                user_agent        TEXT,
                context_metadata  TEXT,
                created_at        TEXT NOT NULL DEFAULT '',
                updated_at        TEXT NOT NULL DEFAULT '',
                sync_status       TEXT NOT NULL DEFAULT 'synced',
                sync_version      INTEGER NOT NULL DEFAULT 1,
                last_synced_at    TEXT,
                sync_hash         TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_audit_logs_org ON audit_logs(organization_id);
        """,
        "strategy": "lww",
        "business_key": [],
        "server_authoritative": True,
    },
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
                created_offline_at    TEXT,
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
        # Sale creation is a command with inventory, batch, ledger, and
        # prescription side effects. Clients must use SyncService._push_sale;
        # a generic CRR row merge would bypass those invariants.
        "server_authoritative": True,
    },
}


def _crr_table_columns(table: str) -> set[str]:
    """Return exact column names declared in the CRR SQLite table DDL."""
    cfg = _CRR_TABLE_CONFIG.get(table)
    if not cfg:
        return set()

    ddl = cfg["ddl"]
    match = re.search(
        rf"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{re.escape(table)}\s*\((.*?)\)\s*;",
        ddl,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return set()

    columns: set[str] = set()
    for line in match.group(1).splitlines():
        line = line.strip().rstrip(",")
        if not line:
            continue
        column_match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s+", line)
        if column_match:
            columns.add(column_match.group(1).lower())
    return columns


def get_crr_table_names() -> List[str]:
    return list(_CRR_TABLE_CONFIG.keys())


def get_crr_config(table: str) -> Optional[Dict[str, Any]]:
    return _CRR_TABLE_CONFIG.get(table)


# ─── Extension resolution (same logic as client db.rs) ────────────────

def _crsqlite_platform_dir() -> Optional[str]:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "linux-x86_64"
    if machine in {"aarch64", "arm64"}:
        return "linux-aarch64"
    return None


def _resolve_extension_path() -> Optional[str]:
    if val := os.environ.get("CRSQLITE_EXTENSION_PATH"):
        if os.path.exists(val):
            return val

    platform_dir = _crsqlite_platform_dir()
    if not platform_dir:
        logger.warning(
            "Unsupported CR-SQLite host architecture: %s",
            platform.machine(),
        )
        return None

    repo_root = Path(__file__).resolve().parents[4]
    backend_root = Path(__file__).resolve().parents[3]
    candidates = [
        repo_root / "crsqlite" / platform_dir / "crsqlite.so",
        backend_root / "crsqlite" / platform_dir / "crsqlite.so",
        Path(__file__).parent / "crsqlite" / platform_dir / "crsqlite.so",
        Path("crsqlite") / platform_dir / "crsqlite.so",
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
        renumbered_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
        db_version_at_creation BIGINT
    );
"""

# P1-7: added after the table may already exist in a deployed database, so a
# plain column in the CREATE above is not enough — this idempotently backfills
# it. Kept as a separate statement (not appended into _AUDIT_LOG_TABLE_DDL)
# because asyncpg only allows multi-statement strings through the simple
# query protocol, which SQLAlchemy uses only for parameter-less executes;
# two explicit execute() calls are the safe way to run sequential DDL here.
_AUDIT_LOG_TABLE_MIGRATE_DDL = """
    ALTER TABLE crr_renumber_audit ADD COLUMN IF NOT EXISTS db_version_at_creation BIGINT;
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
    await db.execute(text(_AUDIT_LOG_TABLE_MIGRATE_DDL))


async def _log_renumbering(
    db: AsyncSession,
    table: str,
    winner_id: str,
    loser_id: str,
    bk_col: str,
    old_bk: str,
    new_bk: str,
    db_version_at_creation: int = 0,
) -> None:
    """Record a renumbering event.

    ``db_version_at_creation`` is the shadow DB's global db_version at the
    moment this event was logged (see ``ShadowDB.get_row_db_version``) — it
    is the ack-bound compaction (P1-7) uses to prune this table, since
    ``crr_renumber_audit`` itself is never pulled by any client and so has
    no other natural sync-safe retention signal.
    """
    event_id = f"{table}:{loser_id}:{old_bk}:{new_bk}"
    await db.execute(
        text("""
            INSERT INTO crr_renumber_audit
                (event_id, table_name, winner_id, loser_id, business_key_col,
                 old_business_key, new_business_key, db_version_at_creation)
            VALUES
                (:event_id, :table_name, :winner_id, :loser_id, :business_key_col,
                 :old_business_key, :new_business_key, :db_version_at_creation)
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
            "db_version_at_creation": db_version_at_creation,
        },
    )


def _shadow_json_default(value: Any) -> str:
    if isinstance(value, (date, datetime, Decimal)):
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    if hasattr(value, "hex"):
        return str(value)
    return str(value)


def _shadow_sqlite_value(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=_shadow_json_default)
    if hasattr(value, "hex") and not isinstance(value, str):
        return str(value)
    return value


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
        self._crr_available = False

    @property
    def crr_available(self) -> bool:
        """Whether cr-sqlite loaded and its change table is queryable."""
        return self._crr_available

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

        extension_loaded = False
        if self._ext_path:
            try:
                conn.enable_load_extension(True)
                # SQLite automatically appends the platform extension suffix
                # (.so on Linux, .dylib on macOS, .dll on Windows) to the
                # extension path, regardless of whether the path already
                # includes it. Strip it if present to avoid double suffixes.
                ext_load_path = self._ext_path
                for suffix in (".so", ".dylib", ".dll"):
                    if ext_load_path.endswith(suffix):
                        ext_load_path = ext_load_path[: -len(suffix)]
                        break
                conn.load_extension(ext_load_path)
                extension_loaded = True
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
        conversion_ok = extension_loaded
        for table_name, cfg in _CRR_TABLE_CONFIG.items():
            conn.executescript(cfg["ddl"])
            if self._ext_path:
                try:
                    conn.execute(f"SELECT crsql_as_crr('{table_name}')")
                    logger.info("Table %s converted to CRR", table_name)
                except Exception as exc:
                    conversion_ok = False
                    logger.warning(
                        "crsql_as_crr for %s failed: %s", table_name, exc
                    )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS crr_server_row_hashes (
                table_name TEXT NOT NULL,
                row_id     TEXT NOT NULL,
                row_hash   TEXT NOT NULL,
                PRIMARY KEY (table_name, row_id)
            )
        """)
        if conversion_ok:
            try:
                conn.execute("SELECT 1 FROM crsql_changes LIMIT 1")
                self._crr_available = True
            except sqlite3.Error as exc:
                logger.error("cr-sqlite change table unavailable: %s", exc)
                self._crr_available = False
        else:
            self._crr_available = False

        if self._crr_available:
            self._init_row_ownership_tracking(conn)

        conn.commit()
        self._conn = conn

    def _init_row_ownership_tracking(self, conn: sqlite3.Connection) -> None:
        """
        Maintain a (table, pk) -> (organization_id, branch_id) index that
        survives row deletion, via triggers on every CRR table.

        ``crsql_changes`` rows carry no tenant information at all — only a
        cr-sqlite-encoded pk. A plain join against the live table can
        attribute an INSERT/UPDATE change to a tenant, but after a hard
        delete (see ``delete_crr_row``) the underlying row is gone, so that
        join would silently drop the delete-tombstone instead of scoping
        it. This index is populated by AFTER INSERT/UPDATE triggers — so it
        covers every write path without every caller needing to remember to
        maintain it — and rows are *never* removed from it, so
        ``get_changes_since`` can still scope tombstones correctly.

        This closes the cross-tenant pull leak documented as finding S1 in
        docs/reviews/2026-08-04-inventory-sync-sales-independent-review.md:
        previously ``get_changes_since`` had no organization/branch filter
        at all, so every client pulled every organization's changes.
        """
        conn.execute("""
            CREATE TABLE IF NOT EXISTS crr_row_owners (
                table_name      TEXT NOT NULL,
                pk              BLOB NOT NULL,
                organization_id TEXT,
                branch_id       TEXT,
                PRIMARY KEY (table_name, pk)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_crr_row_owners_org "
            "ON crr_row_owners(table_name, organization_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_crr_row_owners_branch "
            "ON crr_row_owners(table_name, branch_id)"
        )

        for table_name in _CRR_TABLE_CONFIG:
            columns = _crr_table_columns(table_name)
            has_org = "organization_id" in columns
            has_branch = "branch_id" in columns
            if not has_org and not has_branch:
                # No tenant column at all on this table — nothing to scope.
                # None of the current CRR tables hit this; skip defensively
                # rather than crash if one is ever added without either.
                continue

            new_org_expr = "NEW.organization_id" if has_org else "NULL"
            new_branch_expr = "NEW.branch_id" if has_branch else "NULL"
            col_org_expr = "organization_id" if has_org else "NULL"
            col_branch_expr = "branch_id" if has_branch else "NULL"

            for event, suffix in (("INSERT", "ai"), ("UPDATE", "au")):
                conn.execute(f"""
                    CREATE TRIGGER IF NOT EXISTS [trg_own_{suffix}_{table_name}]
                    AFTER {event} ON [{table_name}]
                    BEGIN
                        INSERT INTO crr_row_owners (table_name, pk, organization_id, branch_id)
                        VALUES ('{table_name}', crsql_pack_columns(NEW.id), {new_org_expr}, {new_branch_expr})
                        ON CONFLICT (table_name, pk) DO UPDATE SET
                            organization_id = excluded.organization_id,
                            branch_id = excluded.branch_id;
                    END;
                """)

            # Backfill rows written before this index existed (or by a path
            # that predates these triggers). Idempotent — safe every startup.
            # The otherwise-redundant WHERE clause works around a SQLite
            # grammar ambiguity: without it, "INSERT ... SELECT ... ON
            # CONFLICT" fails to parse because the parser can't tell ON
            # is introducing the upsert clause rather than a join.
            conn.execute(f"""
                INSERT INTO crr_row_owners (table_name, pk, organization_id, branch_id)
                SELECT '{table_name}', crsql_pack_columns(id), {col_org_expr}, {col_branch_expr}
                FROM [{table_name}]
                WHERE 1 = 1
                ON CONFLICT (table_name, pk) DO UPDATE SET
                    organization_id = excluded.organization_id,
                    branch_id = excluded.branch_id
            """)

    async def get_all_shadow_row_hashes(self, table: str) -> dict[str, str]:
        """Fetch all row IDs and their server row hashes from a shadow DB table."""
        def _sync() -> dict[str, str]:
            with self._lock:
                conn = self._conn
                if conn is None:
                    return {}
                try:
                    rows = conn.execute(
                        "SELECT row_id, row_hash FROM crr_server_row_hashes WHERE table_name = ?",
                        (table,)
                    ).fetchall()
                    return {str(r[0]): str(r[1]) for r in rows}
                except Exception:
                    return {}
        return await asyncio.to_thread(_sync)

    async def publish_server_authoritative_tables(
        self,
        db: AsyncSession,
        organization_id: Any,
        branch_id: Any = None,
    ) -> int:
        """Publish server-owned and client-writable Postgres rows into the CRR shadow peer.

        Makes both Postgres server-authoritative catalog changes and any other server-created
        client-writable data visible to CRR clients so they can be synced completely.
        """
        published = 0
        for table, cfg in _CRR_TABLE_CONFIG.items():
            columns = _crr_table_columns(table)

            if "organization_id" in columns and "branch_id" in columns and branch_id:
                stmt = f'SELECT * FROM "{table}" WHERE organization_id = :organization_id AND branch_id = :branch_id'
                params = {"organization_id": str(organization_id), "branch_id": str(branch_id)}
            elif "organization_id" in columns:
                stmt = f'SELECT * FROM "{table}" WHERE organization_id = :organization_id'
                params = {"organization_id": str(organization_id)}
            elif "branch_id" in columns and branch_id:
                stmt = f'SELECT * FROM "{table}" WHERE branch_id = :branch_id'
                params = {"branch_id": str(branch_id)}
            else:
                continue

            result = await db.execute(text(stmt), params)
            
            existing_hashes = await self.get_all_shadow_row_hashes(table)

            for row in result.mappings().all():
                row_dict = dict(row)
                
                # Compute hash in python memory to check if anything changed before calling SQLite
                shadow_row = {
                    column: _shadow_sqlite_value(row_dict.get(column))
                    for column in columns
                    if column in row_dict
                }
                if "id" not in shadow_row or not shadow_row["id"]:
                    continue
                normalized = json.dumps(
                    shadow_row,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=_shadow_json_default,
                )
                row_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
                
                if existing_hashes.get(str(shadow_row["id"])) == row_hash:
                    continue
                    
                if await self.upsert_shadow_server_row(table, row_dict):
                    published += 1
        return published

    async def upsert_shadow_server_row(self, table: str, row: Dict[str, Any]) -> bool:
        """Upsert one Postgres row into shadow SQLite if its content changed."""
        if table not in _CRR_TABLE_CONFIG:
            raise ValueError(f"Unknown CRR table: {table}")

        def _sync() -> bool:
            with self._lock:
                conn = self._conn
                if conn is None:
                    raise RuntimeError("Shadow DB not initialised")
                table_columns = [
                    r[1] for r in conn.execute(f"PRAGMA table_info([{table}])").fetchall()
                    if r[1] != "rowid"
                ]
                shadow_row = {
                    column: _shadow_sqlite_value(row.get(column))
                    for column in table_columns
                    if column in row
                }
                if "id" not in shadow_row or not shadow_row["id"]:
                    return False
                normalized = json.dumps(
                    shadow_row,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=_shadow_json_default,
                )
                row_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
                current = conn.execute(
                    """SELECT row_hash FROM crr_server_row_hashes
                       WHERE table_name = ? AND row_id = ?""",
                    (table, str(shadow_row["id"])),
                ).fetchone()
                if current and current[0] == row_hash:
                    return False

                columns = list(shadow_row.keys())
                placeholders = ", ".join("?" for _ in columns)
                updates = ", ".join(
                    f"[{column}] = excluded.[{column}]"
                    for column in columns
                    if column != "id"
                )
                conn.execute(
                    f"""INSERT INTO [{table}] ({', '.join(f'[{c}]' for c in columns)})
                        VALUES ({placeholders})
                        ON CONFLICT(id) DO UPDATE SET {updates}""",
                    [shadow_row[column] for column in columns],
                )
                conn.execute(
                    """INSERT INTO crr_server_row_hashes(table_name, row_id, row_hash)
                       VALUES (?, ?, ?)
                       ON CONFLICT(table_name, row_id) DO UPDATE SET row_hash = excluded.row_hash""",
                    (table, str(shadow_row["id"]), row_hash),
                )
                conn.commit()
                return True

        return await asyncio.to_thread(_sync)

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

    async def resolve_row_id(self, table: str, encoded_pk: Any) -> Optional[str]:
        """Resolve cr-sqlite's encoded PK blob to the table's textual id."""
        if table not in _CRR_TABLE_CONFIG:
            raise ValueError(f"Unknown CRR table: {table}")

        def _sync() -> Optional[str]:
            with self._lock:
                conn = self._conn
                if conn is None:
                    raise RuntimeError("Shadow DB not initialised")
                row = conn.execute(
                    f"SELECT id FROM [{table}] "
                    "WHERE crsql_pack_columns(id) = ? LIMIT 1",
                    (encoded_pk,),
                ).fetchone()
                return str(row[0]) if row else None

        return await asyncio.to_thread(_sync)

    async def pk_is_tombstoned(self, table: str, encoded_pk: Any) -> bool:
        """True if this encoded pk's current clock state is a delete.

        `resolve_row_id` returning None is ambiguous: it means either the
        pk was never materialized (a real problem) or the row legitimately
        no longer exists. The latter is common and expected — `sum_and_merge`
        and the customer-merge strategy both call `delete_crr_row` to retire
        a losing duplicate's id after folding it into the winner (see that
        method's docstring), and a genuine client-authored delete looks
        identical. Because a client only advances its push cursor once a
        *whole* batch succeeds (pushCrr()'s all-or-nothing gating), an
        already-processed row like this gets resent verbatim on every retry
        as long as anything else in the same batch keeps failing — cr-sqlite
        correctly refuses to resurrect it (the replayed create loses the
        causality race against the already-applied delete), so without this
        check `resolve_row_id` would report the same permanent, unrecoverable
        "error" for it forever. `crsql_changes` reflects current clock state,
        not a full history, so a pk whose changes collapsed into cr-sqlite's
        single tombstone row (`cid = '-1'`, the same sentinel already relied
        on by `compact_crr_tables`) reads back as tombstoned even after this
        push's stale create-columns were just re-inserted alongside it.
        """
        if table not in _CRR_TABLE_CONFIG:
            raise ValueError(f"Unknown CRR table: {table}")

        def _sync() -> bool:
            with self._lock:
                conn = self._conn
                if conn is None:
                    raise RuntimeError("Shadow DB not initialised")
                row = conn.execute(
                    """SELECT cid FROM crsql_changes
                       WHERE "table" = ? AND pk = ?
                       ORDER BY db_version DESC, seq DESC LIMIT 1""",
                    (table, encoded_pk),
                ).fetchone()
                return row is not None and row[0] == "-1"

        return await asyncio.to_thread(_sync)

    async def restore_rejected_row(
        self,
        table: str,
        row_id: str,
        authoritative_row: Optional[Dict[str, Any]],
    ) -> None:
        """Remove a rejected merge and restore the last Postgres state.

        A CRR push reaches the shadow database before application-level
        validation. Rejected changes must not remain eligible for pull or
        scheduled reconciliation. Deleting and, when available, reinserting
        the authoritative Postgres row creates a newer corrective CRR state.
        """
        if table not in _CRR_TABLE_CONFIG:
            raise ValueError(f"Unknown CRR table: {table}")

        def _sqlite_value(value: Any) -> Any:
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, (date, datetime, Decimal)):
                return str(value)
            return str(value) if hasattr(value, "hex") and not isinstance(value, str) else value

        def _sync() -> None:
            with self._lock:
                conn = self._conn
                if conn is None:
                    raise RuntimeError("Shadow DB not initialised")
                conn.execute("SAVEPOINT reject_crr_group")
                try:
                    conn.execute(f"DELETE FROM [{table}] WHERE id = ?", (row_id,))
                    if authoritative_row:
                        table_columns = {
                            str(info[1])
                            for info in conn.execute(
                                f"PRAGMA table_info([{table}])"
                            ).fetchall()
                        }
                        restored = {
                            key: _sqlite_value(value)
                            for key, value in authoritative_row.items()
                            if key in table_columns and value is not None
                        }
                        restored["id"] = row_id
                        columns = list(restored)
                        placeholders = ", ".join("?" for _ in columns)
                        conn.execute(
                            f"INSERT INTO [{table}] "
                            f"({', '.join(f'[{column}]' for column in columns)}) "
                            f"VALUES ({placeholders})",
                            [restored[column] for column in columns],
                        )
                    conn.execute("RELEASE reject_crr_group")
                    conn.commit()
                except Exception:
                    conn.execute("ROLLBACK TO reject_crr_group")
                    conn.execute("RELEASE reject_crr_group")
                    raise

        await asyncio.to_thread(_sync)

    # ── List all rows that changed in crsql_changes (for pull) ────────

    async def get_changes_since(
        self,
        organization_id: str,
        org_branch_ids: Optional[List[str]] = None,
        since_db_version: int = 0,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """Return crsql_changes rows with db_version > since_db_version,
        scoped to a single organization.

        This is what the client pulls to apply server-side changes locally.

        ``organization_id`` is mandatory: without it this would return every
        tenant's changes to every client (see S1 in
        docs/reviews/2026-08-04-inventory-sync-sales-independent-review.md).
        Rows are attributed via the ``crr_row_owners`` index maintained by
        ``_init_row_ownership_tracking`` — a row belongs to this org if
        either its own ``organization_id`` matches, or (for tables that
        only carry ``branch_id``, e.g. branch_inventory/drug_batches) its
        branch is one of this org's branches, passed in via
        ``org_branch_ids``.
        """
        if not self._crr_available:
            raise RuntimeError("Server CRR sync is unavailable")

        branch_ids = list(org_branch_ids or [])

        def _sync() -> List[Dict[str, Any]]:
            with self._lock:
                conn = self._conn
                if conn is None:
                    raise RuntimeError("Shadow DB not initialised")
                conn.row_factory = sqlite3.Row

                owner_match = "o.organization_id = ?"
                params: List[Any] = [organization_id]
                if branch_ids:
                    placeholders = ", ".join("?" for _ in branch_ids)
                    owner_match += f" OR (o.organization_id IS NULL AND o.branch_id IN ({placeholders}))"
                    params.extend(branch_ids)

                query = f"""
                    SELECT c."table", c.pk, c.cid, c.val, c.col_version, c.db_version,
                           c.site_id, c.cl, c.seq
                    FROM crsql_changes c
                    JOIN crr_row_owners o
                      ON o.table_name = c."table" AND o.pk = c.pk
                    WHERE c.db_version > ?
                      AND ({owner_match})
                    ORDER BY c.db_version, c.seq
                    LIMIT ?
                """
                cur = conn.execute(
                    query,
                    [since_db_version, *params, limit],
                )
                return [dict(r) for r in cur.fetchall()]

        return await asyncio.to_thread(_sync)

    # ── Get max db_version (for pull pagination) ──────────────────────

    async def max_db_version(self) -> int:
        if not self._crr_available:
            raise RuntimeError("Server CRR sync is unavailable")

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

    async def get_row_db_version(self, table: str, row_id: str) -> int:
        """Highest db_version currently recorded for a single row's pk.

        Used by P1-7 to stamp ``crr_renumber_audit`` rows with an
        ack-boundable version at the moment they are written (see
        ``_log_renumbering``), since that table has no other sync-derived
        retention signal.
        """
        if not self._crr_available:
            return 0

        def _sync() -> int:
            with self._lock:
                conn = self._conn
                if conn is None:
                    return 0
                pk_row = conn.execute(
                    f"SELECT crsql_pack_columns(id) FROM [{table}] WHERE id = ?",
                    (row_id,),
                ).fetchone()
                if pk_row is None:
                    return 0
                cur = conn.execute(
                    'SELECT COALESCE(MAX(db_version), 0) FROM crsql_changes '
                    'WHERE "table" = ? AND pk = ?',
                    (table, pk_row[0]),
                )
                return cur.fetchone()[0] or 0

        return await asyncio.to_thread(_sync)

    # ── P1-7: ack-bounded compaction ───────────────────────────────────
    #
    # SPIKE-VALIDATED SAFETY MODEL (kept permanently as
    # tests/unit/test_sync_service.py::test_crr_clock_compaction_is_safe —
    # see plan P1-7 Step 0). Two facts were confirmed against the real
    # vendored crsqlite.so v0.16.3 before this was written, and both
    # surprised the original plan, which assumed __crsql_clock was an
    # append-only per-edit history log:
    #
    #  1. __crsql_clock stores exactly ONE physical row per live
    #     (pk, col_name) — cr-sqlite UPSERTs it in place on every write.
    #     There is no "superseded older version" to prune for a live
    #     cell; that one row *is* the cell's only recorded value, and
    #     deleting it would permanently and silently lose that column's
    #     value for any client that has not yet done a full initial pull
    #     (e.g. a brand-new branch). So compaction never touches rows
    #     that represent a live cell's current value — only delete-
    #     tombstones are eligible.
    #
    #  2. On DELETE, cr-sqlite consolidates every one of a row's
    #     per-column clock rows into a SINGLE tombstone row, identified
    #     by the sentinel ``col_name = '-1'``. That tombstone is the only
    #     thing this method ever removes.
    #
    #  3. (The dangerous one.) db_version is ONE counter shared globally
    #     across every CRR table in the shadow DB (confirmed by writing
    #     to two independent tables and observing db_version advance
    #     monotonically across both), and cr-sqlite derives "what's the
    #     next db_version" from whatever is still physically present in
    #     __crsql_clock — it is not an independent counter that survives
    #     rows being deleted out from under it. Deleting *every* row that
    #     currently holds the shadow DB's global max db_version causes
    #     the next write ANYWHERE in the database to be assigned a LOWER
    #     db_version than one already handed out and pulled by clients —
    #     which silently breaks `db_version > since_db_version` pulls
    #     forever after (already-synced clients would never see it,
    #     because it would look like something they'd already received).
    #     This method therefore NEVER deletes a row whose db_version is
    #     >= the shadow DB's global max at the start of the compaction
    #     pass, on top of the ack-bound watermark check.

    async def compact_crr_tables(self, watermark: int) -> Dict[str, int]:
        """Prune fully-acknowledged delete-tombstones from every CRR
        table's physical ``__crsql_clock`` shadow table.

        ``watermark`` is the global minimum ``last_acked_db_version``
        across active branches (see ``main.py::_compute_active_branch_min_watermark``)
        — i.e. every active branch has confirmed receiving every change up
        to and including this version. A tombstone is deleted only when
        BOTH:
          - its db_version <= watermark (every active branch already has it), and
          - its db_version < the shadow DB's current global max db_version
            (never delete the row(s) holding the live high-water mark —
            see safety note 3 above).

        Returns ``{table_name: rows_deleted}`` for tables that had
        anything to prune.
        """
        if not self._crr_available or watermark <= 0:
            return {}

        def _sync() -> Dict[str, int]:
            with self._lock:
                conn = self._conn
                if conn is None:
                    return {}
                global_max = (
                    conn.execute(
                        "SELECT COALESCE(MAX(db_version), 0) FROM crsql_changes"
                    ).fetchone()[0]
                    or 0
                )
                deleted: Dict[str, int] = {}
                for table in _CRR_TABLE_CONFIG:
                    clock_table = f"{table}__crsql_clock"
                    try:
                        cur = conn.execute(
                            f"""DELETE FROM [{clock_table}]
                                WHERE col_name = '-1'
                                  AND db_version <= ?
                                  AND db_version < ?""",
                            (watermark, global_max),
                        )
                    except sqlite3.Error as exc:
                        logger.warning(
                            "CRR compaction: skipping %s (%s)", clock_table, exc
                        )
                        continue
                    if cur.rowcount:
                        deleted[table] = cur.rowcount
                conn.commit()
                return deleted

        return await asyncio.to_thread(_sync)

    async def get_prunable_row_ids(
        self, table: str, watermark: int, limit: int = 500
    ) -> List[str]:
        """Live row ids in a server-authoritative CRR table whose most
        recent db_version is <= watermark — i.e. every active branch has
        already pulled this row's current state at least once, so it is
        safe to delete both from Postgres and (via ``delete_crr_row``)
        from the shadow DB. Never returns a row that has not yet been
        published into the shadow DB at all (fail closed: no shadow
        record means we cannot prove anyone has received it).
        """
        if not self._crr_available or watermark <= 0:
            return []

        def _sync() -> List[str]:
            with self._lock:
                conn = self._conn
                if conn is None:
                    return []
                pk_rows = conn.execute(
                    """SELECT DISTINCT pk FROM crsql_changes
                       WHERE "table" = ? AND db_version > 0 AND db_version <= ?
                       LIMIT ?""",
                    (table, watermark, limit),
                ).fetchall()
                ids: List[str] = []
                for (pk,) in pk_rows:
                    row = conn.execute(
                        f"SELECT id FROM [{table}] WHERE crsql_pack_columns(id) = ?",
                        (pk,),
                    ).fetchone()
                    if row is not None:
                        ids.append(str(row[0]))
                return ids

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
        row = self._prepare_pg_row(table, row)
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

    @classmethod
    def _prepare_pg_row(cls, table: str, row: Dict[str, Any]) -> Dict[str, Any]:
        """Remove local-only fields and normalize required Postgres values."""
        model_table = Base.metadata.tables.get(table)
        postgres_columns = (
            {column.name for column in model_table.columns}
            if model_table is not None else None
        )
        prepared = {
            key: value
            for key, value in row.items()
            if key not in ("rowid", "sync_status", "synced_at")
            and (postgres_columns is None or key in postgres_columns)
        }
        if not prepared:
            return prepared
        # Client queue state is local-only. Raw SQL upserts do not apply ORM
        # defaults, so provide the required server-side state explicitly.
        prepared["sync_status"] = "synced"
        return cls._coerce_pg_types(table, prepared)

    @staticmethod
    def _coerce_pg_types(table: str, row: Dict[str, Any]) -> Dict[str, Any]:
        """Convert shadow SQLite scalar values to SQLAlchemy/asyncpg types."""
        coerced = dict(row)
        model_table = Base.metadata.tables.get(table)
        if model_table is None:
            return coerced

        for column in model_table.columns:
            if column.name not in coerced or coerced[column.name] is None:
                continue
            value = coerced[column.name]
            column_type = column.type

            if isinstance(column_type, DateTime) and isinstance(value, str):
                coerced[column.name] = (
                    datetime.fromisoformat(value.replace("Z", "+00:00"))
                    if value
                    else None
                )
            elif isinstance(column_type, Date) and isinstance(value, str):
                coerced[column.name] = date.fromisoformat(value[:10]) if value else None
            elif isinstance(column_type, Boolean) and not isinstance(value, bool):
                if isinstance(value, str):
                    coerced[column.name] = value.strip().lower() in {
                        "1", "true", "t", "yes", "y",
                    }
                else:
                    coerced[column.name] = bool(value)
            elif isinstance(column_type, Integer) and not isinstance(value, int):
                coerced[column.name] = int(value)
            elif isinstance(column_type, Numeric) and not isinstance(value, Decimal):
                coerced[column.name] = Decimal(str(value))
            elif isinstance(column_type, Float) and not isinstance(value, float):
                coerced[column.name] = float(value)

        return coerced

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
        merged = self._coerce_pg_types(table, merged)

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
                SELECT DISTINCT "{bk_column}" FROM {table}
                WHERE "{bk_column}" LIKE :pattern
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
        incoming_row = self._coerce_pg_types(table, incoming_row)

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
        # Captured *after* the business-key update above so the recorded
        # version reflects the write that just happened, giving P1-7
        # compaction a real ack-bound to prune this event by later.
        loser_db_version = await self.get_row_db_version(table, loser_id)
        await _ensure_audit_table(db)
        await _log_renumbering(
            db, table, winner_id, loser_id,
            bk_column, old_bk, new_bk,
            db_version_at_creation=loser_db_version,
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
        cfg = _CRR_TABLE_CONFIG.get(table, {})
        if cfg.get("server_authoritative"):
            # These tables are published Postgres -> shadow for CRR delivery.
            # Replaying their intentionally narrow shadow rows back into
            # Postgres can violate server-only NOT NULL columns.
            return checked, 0

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
