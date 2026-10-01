#!/usr/bin/env python3
"""
List the user names present in the database.

Read-only. Reads DATABASE_URL from backend.laso/.env.

Usage:
  PYTHONPATH=. python3 scripts/list_users.py
  PYTHONPATH=. python3 scripts/list_users.py --json
  PYTHONPATH=. python3 scripts/list_users.py --all       # include soft-deleted
  PYTHONPATH=. python3 scripts/list_users.py --username admin
"""

import sys
import os
import json
import asyncio
import argparse
import logging
from typing import Dict, List

os.environ.setdefault("ENVIRONMENT", "development")

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.user.user_model import User, UserRole, Role


def quiet_engine_echo() -> None:
    """Silence SQLAlchemy's statement echo.

    Two emit paths exist and only logging.disable covers both:
      - BEGIN/COMMIT/ROLLBACK go through _should_log_info(), which honours the
        level set on the 'sqlalchemy.engine.Engine' logger.
      - Statement text goes through _log_info() -> InstanceLogger.log(), which
        maps echo=True to INFO and calls logger._log() directly, bypassing
        isEnabledFor. Set level is ignored there (sqlalchemy/log.py:197-210).
    logging.manager.disable is the single threshold both paths check.
    """
    for name in ("sqlalchemy.engine.Engine", "sqlalchemy.engine"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.disable(logging.INFO)


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="List usernames in the database.")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    p.add_argument(
        "--all",
        action="store_true",
        dest="include_deleted",
        help="Include soft-deleted users.",
    )
    p.add_argument(
        "--username",
        default=None,
        help="Case-insensitive exact username filter.",
    )
    return p.parse_args(argv)


async def fetch_users(args: argparse.Namespace) -> List[Dict]:
    quiet_engine_echo()

    stmt = select(User, Role.name).outerjoin(
        UserRole, UserRole.user_id == User.id
    ).outerjoin(Role, Role.id == UserRole.role_id)

    if not args.include_deleted:
        stmt = stmt.where(User.is_deleted.is_(False))
    if args.username:
        stmt = stmt.where(User.username.ilike(args.username))

    stmt = stmt.order_by(User.username)

    rows = []
    async with AsyncSessionLocal() as db:
        result = await db.execute(stmt)
        for user, role_name in result.unique().all():
            rows.append(
                {
                    "id": str(user.id),
                    "username": user.username,
                    "full_name": user.full_name,
                    "email": user.email,
                    "roles": role_name,
                    "is_active": user.is_active,
                    "is_super_admin": user.is_super_admin,
                    "is_deleted": user.is_deleted,
                }
            )
    return rows


def render_table(rows: List[Dict]) -> str:
    if not rows:
        return "No users found."

    cols = ["username", "full_name", "roles", "is_active", "is_super_admin", "is_deleted"]
    headers = {
        "username": "USERNAME",
        "full_name": "FULL NAME",
        "roles": "ROLES",
        "is_active": "ACTIVE",
        "is_super_admin": "SUPER",
        "is_deleted": "DELETED",
    }
    widths = {
        c: max(len(headers[c]), *(len(str(r.get(c))) for r in rows)) for c in cols
    }

    lines = ["  ".join(headers[c].ljust(widths[c]) for c in cols)]
    lines.append("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        lines.append("  ".join(str(r.get(c)).ljust(widths[c]) for c in cols))
    lines.append("")
    lines.append(f"{len(rows)} user(s).")
    return "\n".join(lines)


async def main(argv: List[str]) -> int:
    args = parse_args(argv)
    rows = await fetch_users(args)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(render_table(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))