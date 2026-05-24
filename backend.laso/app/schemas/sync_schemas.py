"""
Sync Schemas
============
Defines the pull/push contract between the Tauri desktop client and the server.

Ownership rules encoded here:
  Pull-only  (org-level, branch never writes):
      drugs, drug_categories, price_contracts, customers

  Push+Pull  (branch-level, branch is source of truth):
      branch_inventory, drug_batches, stock_adjustments,
      sales, purchase_orders

  Special:
      customers  — org-level but branches CREATE them offline,
                   then push; server deduplicates by phone/email.

  Not synced (server-only):
      insurance_providers, users, suppliers, prescriptions
      — these are managed exclusively through the web admin and
        are not needed offline by the desktop client.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import Field, field_validator
import uuid

from app.schemas.base_schemas import BaseSchema
from app.schemas.drugs_schemas import DrugResponse, DrugCategoryResponse
from app.schemas.inventory_schemas import (
    BranchInventoryResponse,
    DrugBatchResponse,
)
from app.schemas.price_contract_schemas import PriceContractResponse
from app.schemas.customer_schemas import CustomerResponse
from app.schemas.sales_schemas import SaleResponse
from app.schemas.purchase_order_schemas import PurchaseOrderResponse


# ──────────────────────────────────────────────────────────────────────────────
# PULL  (server → branch)
# ──────────────────────────────────────────────────────────────────────────────

class PullRequest(BaseSchema):
    """
    Branch requests all records changed since last_sync_at.
    Pass None on first sync to get the full dataset.
    """
    branch_id: uuid.UUID = Field(..., description="Requesting branch")
    last_sync_at: Optional[datetime] = Field(
        None,
        description="ISO timestamp of last successful pull. "
                    "Omit for initial full sync.",
    )
    # Client can limit which tables it wants (useful for partial refreshes)
    tables: List[str] = Field(
        default=[
            "drugs",
            "drug_categories",
            "price_contracts",
            "customers",
            # branch-owned tables are also pulled so multi-device branches stay in sync
            "branch_inventory",
            "drug_batches",
            "sales",
            "purchase_orders",
        ],
        description="Subset of tables to pull. Defaults to all.",
    )

    @field_validator("tables")
    @classmethod
    def validate_tables(cls, v: List[str]) -> List[str]:
        allowed = {
            "drugs",
            "drug_categories",
            "price_contracts",
            "customers",
            "branch_inventory",
            "drug_batches",
            "stock_adjustments",
            "sales",
            "purchase_orders",
        }
        unknown = set(v) - allowed
        if unknown:
            raise ValueError(f"Unknown tables requested: {unknown}")
        return v


class PullResponse(BaseSchema):
    """
    Delta payload returned to the branch.
    Each list contains only records updated *after* last_sync_at.
    Deleted records are included with sync_status='deleted' so the
    client knows to remove them locally.
    """
    # Org-level (pull-only on client)
    drugs: List[DrugResponse] = Field(default_factory=list)
    drug_categories: List[DrugCategoryResponse] = Field(default_factory=list)
    price_contracts: List[PriceContractResponse] = Field(default_factory=list)
    customers: List[CustomerResponse] = Field(default_factory=list)

    # Branch-level (also pulled so multi-device branches stay in sync)
    branch_inventory: List[BranchInventoryResponse] = Field(default_factory=list)
    drug_batches: List[DrugBatchResponse] = Field(default_factory=list)
    sales: List[SaleResponse] = Field(default_factory=list)
    purchase_orders: List[PurchaseOrderResponse] = Field(default_factory=list)

    # Metadata
    sync_timestamp: datetime = Field(
        description="Server timestamp of this pull. "
                    "Store and send as last_sync_at on the next pull."
    )
    has_more: bool = Field(
        default=False,
        description="True when the delta is large and was paginated. "
                    "Pull again with the same last_sync_at until False.",
    )
    total_records: int = Field(
        default=0,
        description="Total number of records included in this response.",
    )


# ──────────────────────────────────────────────────────────────────────────────
# PUSH  (branch → server)
# ──────────────────────────────────────────────────────────────────────────────

class PushRecord(BaseSchema):
    """
    A single record the branch wants to push to the server.
    The server uses `table_name` to route it to the right handler.
    `local_id` is the UUID the client assigned offline.
    `sync_version` is used for optimistic concurrency: if the server
    already has a higher version, a conflict is raised.
    """
    table_name: str = Field(
        ...,
        description="One of: branch_inventory, drug_batches, "
                    "stock_adjustments, sales, purchase_orders, customers",
    )
    local_id: str = Field(..., description="Client-side UUID for this record")
    operation: str = Field(..., pattern="^(create|update|delete)$")
    sync_version: int = Field(..., ge=1, description="Client's version of this record")
    data: Dict[str, Any] = Field(..., description="Full record payload")
    created_offline_at: datetime = Field(
        ..., description="When the client created/modified this record"
    )

    @field_validator("table_name")
    @classmethod
    def validate_table(cls, v: str) -> str:
        pushable = {
            "branch_inventory",
            "drug_batches",
            "stock_adjustments",
            "sales",
            "purchase_orders",
            "customers",      # special: org-level but created offline
        }
        if v not in pushable:
            raise ValueError(
                f"'{v}' is not a pushable table. "
                f"Org-level tables (drugs, contracts, etc.) are read-only on the client."
            )
        return v


class PushRequest(BaseSchema):
    """
    Batch of pending records the branch is pushing after reconnecting.
    Max 500 records per request — client should chunk larger backlogs.
    """
    branch_id: uuid.UUID
    records: List[PushRecord] = Field(..., min_length=1, max_length=500)

    @field_validator("records")
    @classmethod
    def validate_no_duplicates(cls, v: List[PushRecord]) -> List[PushRecord]:
        seen = set()
        for r in v:
            key = (r.table_name, r.local_id)
            if key in seen:
                raise ValueError(
                    f"Duplicate record in push batch: "
                    f"table={r.table_name} id={r.local_id}"
                )
            seen.add(key)
        return v


class PushConflict(BaseSchema):
    """
    Returned when the server's sync_version is higher than what the
    client sent — meaning another device already updated this record.
    The client receives the server's current version and must resolve.
    """
    local_id: str
    table_name: str
    local_version: int
    server_version: int
    server_record: Dict[str, Any] = Field(
        description="Server's current state of the record"
    )
    resolution: str = Field(
        description=(
            "server_wins   — server record is authoritative (sales, inventory)\n"
            "local_wins    — client record replaces server (rare, manual only)\n"
            "manual_required — human must resolve (e.g. duplicate customer)\n"
        ),
        pattern="^(server_wins|local_wins|manual_required)$",
    )


class PushResult(BaseSchema):
    """Result for a single pushed record."""
    local_id: str
    table_name: str
    server_id: Optional[str] = None
    success: bool
    error: Optional[str] = None


class PushResponse(BaseSchema):
    """
    Full response to a push batch.
    The client should:
      1. Mark `accepted` records as sync_status='synced'
      2. For `conflicts` with server_wins: overwrite local with server_record
      3. For `conflicts` with manual_required: surface to user
      4. For `failed`: retry or surface error
    """
    accepted: List[PushResult] = Field(default_factory=list)
    conflicts: List[PushConflict] = Field(default_factory=list)
    failed: List[PushResult] = Field(default_factory=list)

    # Summary
    total_received: int
    total_accepted: int
    total_conflicts: int
    total_failed: int
    sync_timestamp: datetime

    # New pull timestamp — client should pull from here after a push
    # so it receives any server-side changes triggered by its push
    # (e.g. inventory totals updated by a sale it just pushed)
    next_pull_timestamp: datetime