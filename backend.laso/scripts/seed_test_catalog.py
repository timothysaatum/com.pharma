#!/usr/bin/env python3
"""Inspect targets and seed a repeat-safe demo drug catalog.

Run from ``backend.laso`` so ``.env`` resolves to the same configuration as the
API server::

    .venv/bin/python scripts/seed_test_catalog.py status
    .venv/bin/python scripts/seed_test_catalog.py seed --username admin
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
from typing import Any, Iterable
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.models.inventory.branch_inventory import BranchInventory  # noqa: E402
from app.models.inventory.inventory_model import Drug, DrugCategory  # noqa: E402
from app.models.pharmacy.pharmacy_model import Branch, Organization  # noqa: E402
from app.models.user.user_model import User  # noqa: E402
from app.services.catalog_seed.service import CatalogSeedService  # noqa: E402

DEFAULT_OUTPUT_DIR = Path("/tmp/drug-catalog-seed")
MAX_SNAPSHOT_ROWS = 100_000
MAX_SNAPSHOT_BYTES = 100 * 1024 * 1024


def _json_value(value: Any) -> Any:
    if isinstance(value, (uuid.UUID, datetime, date, Decimal)):
        return str(value)
    return value


def _normalise_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: _json_value(value) for key, value in row.items()} for row in rows]


async def _target_rows(db: AsyncSession) -> list[dict[str, Any]]:
    organizations = list(
        (
            await db.scalars(
                select(Organization)
                .where(Organization.is_active.is_(True))
                .order_by(Organization.name, Organization.id)
            )
        ).all()
    )
    result: list[dict[str, Any]] = []
    for organization in organizations:
        users = list(
            (
                await db.scalars(
                    select(User)
                    .where(
                        User.organization_id == organization.id,
                        User.is_active.is_(True),
                        User.is_deleted.is_(False),
                    )
                    .order_by(User.last_login.desc().nullslast(), User.username)
                )
            ).all()
        )
        branch_count = await db.scalar(
            select(func.count(Branch.id)).where(
                Branch.organization_id == organization.id,
                Branch.is_active.is_(True),
                Branch.is_deleted.is_(False),
            )
        )
        drug_count = await db.scalar(
            select(func.count(Drug.id)).where(
                Drug.organization_id == organization.id,
                Drug.is_deleted.is_(False),
            )
        )
        result.append(
            {
                "organization_id": str(organization.id),
                "organization_name": organization.name,
                "active_branch_count": branch_count or 0,
                "visible_drug_count": drug_count or 0,
                "users": [
                    {
                        "username": user.username,
                        "last_login": _json_value(user.last_login),
                        "assigned_branch_ids": [
                            str(item) for item in user.assigned_branches
                        ],
                    }
                    for user in users
                ],
            }
        )
    return result


async def _resolve_organization_id(
    db: AsyncSession,
    organization_id: str | None,
    username: str | None,
) -> uuid.UUID:
    if organization_id:
        parsed = uuid.UUID(organization_id)
        if await db.get(Organization, parsed) is None:
            raise ValueError(f"Organization {parsed} does not exist")
        return parsed
    if username:
        users = list(
            (
                await db.scalars(
                    select(User).where(
                        User.username == username,
                        User.is_active.is_(True),
                        User.is_deleted.is_(False),
                    )
                )
            ).all()
        )
        if not users:
            raise ValueError(f"Active user '{username}' does not exist")
        if len(users) > 1:
            raise ValueError(
                f"Username '{username}' belongs to multiple organizations; "
                "use --organization-id"
            )
        return users[0].organization_id
    raise ValueError("Choose the UI tenant with --username or --organization-id")


async def _catalog_snapshot(
    db: AsyncSession, organization_id: uuid.UUID
) -> list[dict[str, Any]]:
    categories = list(
        (
            await db.execute(
                select(*DrugCategory.__table__.c).where(
                    DrugCategory.organization_id == organization_id
                )
            )
        ).mappings()
    )
    drugs = list(
        (
            await db.execute(
                select(*Drug.__table__.c).where(Drug.organization_id == organization_id)
            )
        ).mappings()
    )
    inventory = list(
        (
            await db.execute(
                select(
                    Branch.organization_id.label("organization_id"),
                    *BranchInventory.__table__.c,
                )
                .join(Branch, Branch.id == BranchInventory.branch_id)
                .where(Branch.organization_id == organization_id)
            )
        ).mappings()
    )
    rows: list[dict[str, Any]] = []
    rows.extend({"record_type": "category", **dict(row)} for row in categories)
    rows.extend({"record_type": "drug", **dict(row)} for row in drugs)
    rows.extend({"record_type": "inventory", **dict(row)} for row in inventory)
    return _normalise_rows(rows)


def _write_progress(output_dir: Path, percent: int, message: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    line = f"{timestamp} | Catalog seed | {percent}% | {message}\n"
    with (output_dir / "progress.log").open("a", encoding="utf-8") as handle:
        handle.write(line)
    print(line, end="", flush=True)


def _write_snapshot(path: Path, rows: list[dict[str, Any]]) -> None:
    if len(rows) > MAX_SNAPSHOT_ROWS:
        raise RuntimeError(
            f"Snapshot has {len(rows)} rows; explicit approval is required "
            f"above {MAX_SNAPSHOT_ROWS}"
        )
    payload = json.dumps(rows, indent=2, sort_keys=True)
    if len(payload.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise RuntimeError("Snapshot exceeds 100 MB; explicit approval is required")
    path.write_text(payload + "\n", encoding="utf-8")


def _write_before_after_csv(
    path: Path,
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
) -> None:
    rows = [
        {"phase": phase, **row}
        for phase, collection in (("before", before), ("after", after))
        for row in collection
    ]
    fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _record_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        kind: sum(row["record_type"] == kind for row in rows)
        for kind in ("category", "drug", "inventory")
    }


def _write_report(
    output_dir: Path,
    result: dict[str, Any],
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
) -> None:
    before_counts = _record_counts(before)
    after_counts = _record_counts(after)
    demo_examples = [
        row
        for row in after
        if row["record_type"] == "drug" and str(row.get("sku", "")).startswith("DEMO-")
    ][:5]
    examples = "\n".join(
        f"| {row['sku']} | {row['name']} | {row['unit_price']} |"
        for row in demo_examples
    )
    category_change = after_counts["category"] - before_counts["category"]
    drug_change = after_counts["drug"] - before_counts["drug"]
    inventory_change = after_counts["inventory"] - before_counts["inventory"]
    category_row = (
        f"| Categories | {before_counts['category']} | "
        f"{after_counts['category']} | {category_change:+d} |"
    )
    drug_row = (
        f"| Drugs | {before_counts['drug']} | "
        f"{after_counts['drug']} | {drug_change:+d} |"
    )
    inventory_row = (
        f"| Branch inventory | {before_counts['inventory']} | "
        f"{after_counts['inventory']} | {inventory_change:+d} |"
    )
    report = f"""# Drug catalog seed report

Verdict: PASS. The canonical demo catalog is present for organization
`{result['organization_name']}` (`{result['organization_id']}`).

| Record type | Before | After | Change |
|---|---:|---:|---:|
{category_row}
{drug_row}
{inventory_row}

Seed outcome: {json.dumps(result, sort_keys=True)}

## Example catalog rows

| SKU | Name | Selling price |
|---|---|---:|
{examples}

The seed is idempotent by organization and SKU. Re-running it updates canonical
demo metadata but preserves existing stock quantities.
"""
    (output_dir / "report.md").write_text(report, encoding="utf-8")


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, echo=False, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as db:
            if args.command == "status":
                print(json.dumps(await _target_rows(db), indent=2, sort_keys=True))
                return 0

            organization_id = await _resolve_organization_id(
                db, args.organization_id, args.username
            )
            output_dir = Path(args.output_dir).resolve()
            if not output_dir.is_relative_to(Path("/tmp")):
                raise ValueError("Seed artifacts must be written under /tmp")
            _write_progress(output_dir, 0, "ETA <1 minute | resolving target")
            before = await _catalog_snapshot(db, organization_id)
            _write_snapshot(output_dir / "snapshot-before.json", before)
            _write_progress(
                output_dir,
                25,
                f"ETA <1 minute | snapshot complete | rows={len(before)}",
            )
            await db.rollback()
            async with db.begin():
                seed_result = await CatalogSeedService.seed(db, organization_id)
            _write_progress(
                output_dir, 75, "ETA <1 minute | database transaction committed"
            )
            after = await _catalog_snapshot(db, organization_id)
            _write_snapshot(output_dir / "catalog-after.json", after)
            _write_before_after_csv(output_dir / "before-after.csv", before, after)
            result = seed_result.as_dict()
            _write_report(output_dir, result, before, after)
            _write_progress(
                output_dir,
                100,
                f"ETA 0s | complete | visible_drugs={_record_counts(after)['drug']}",
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            print(f"Artifacts: {output_dir}")
            return 0
    finally:
        await engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "status", help="List UI tenant targets and current drug counts"
    )
    seed_parser = subparsers.add_parser("seed", help="Seed the canonical demo catalog")
    selector = seed_parser.add_mutually_exclusive_group(required=True)
    selector.add_argument(
        "--username", help="Seed the organization belonging to this UI user"
    )
    selector.add_argument("--organization-id", help="Seed this organization UUID")
    seed_parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"Snapshot and report directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    return parser


def main() -> int:
    try:
        return asyncio.run(_run(_parser().parse_args()))
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
