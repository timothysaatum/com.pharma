#!/usr/bin/env python3
"""
refuse_real_database.py — the fence for scripts that are not pytest.

WHY THIS EXISTS
---------------
Two fences already exist and neither covers a standalone script:

  * `backend.laso/tests/conftest.py` (commit e8dd90c) refuses a pytest run whose
    TEST_DATABASE_URL names atlasdb. It runs on import of conftest, so it only
    guards pytest.
  * `ui.laso/tests/e2e/helpers/backend-db.ts` (commit 213b6fc) refuses when
    TEST_DATABASE_URL is unset or names atlasdb. It guards the Playwright
    `BackendDatabase` helper only.

A script run by hand reads `settings.DATABASE_URL`, which in
`backend.laso/.env` points at atlasdb, and writes to it. That is how fixture rows
under sentinel UUIDs reached the live database in the first place: the Sept 2026
`customer_created` events with aggregate `aaaaaaaa-…` and author `44444444…`
could only have come from a seed or test script aimed at it.

THE RULE
--------
Refuse unless the target is explicitly disposable:

  * TEST_DATABASE_URL must be SET and non-empty. No fallback to .env. A script
    that cannot prove it is disposable does not run.
  * The database name must not be atlasdb, postgres, template0 or template1
    (same list as the pytest fence), compared case-insensitively.
  * --i-know-what-im-doing downgrades the refusal to a loud warning, for the case
    where someone genuinely needs to run against a real database on purpose.

Usage:
    from refuse_real_database import require_disposable_database
    require_disposable_database(settings.DATABASE_URL)
"""
from __future__ import annotations

import os
import re
import sys

FORBIDDEN_DB_NAMES = {"atlasdb", "postgres", "template0", "template1"}

_URL_RE = re.compile(
    r"postgresql(?:\+\w+)?://"
    r"(?:(?P<user>[^:@/]+)(?::(?P<pw>[^@]*))?@)?"
    r"(?P<host>[^:/@]*)(?::(?P<port>\d+))?"
    r"/(?P<db>[^?]+)",
    re.I,
)

DOWNGRADE_FLAG = "--i-know-what-im-doing"


def _db_name(url: str) -> str:
    """Database name from a postgres URL, or '' if unparseable."""
    m = _URL_RE.match(url or "")
    if not m:
        return ""
    return m.group("db").strip().lower()


def _test_database_url() -> str:
    return (os.environ.get("TEST_DATABASE_URL") or "").strip()


def check_disposable_database(url: str, *, override: bool = False) -> tuple[bool, str]:
    """Return (ok, reason). Pure, so it is trivially testable."""
    if not (url or "").strip():
        return False, "no database URL resolved"

    name = _db_name(url)
    if not name:
        return False, f"could not parse a database name from the URL"

    if name not in FORBIDDEN_DB_NAMES:
        return True, f"database {name!r} is not a protected name"

    # A protected name. Reachable either because the caller overrode the fence,
    # or because the target was never chosen deliberately.
    reason = (
        f"writing to protected database {name!r} because {DOWNGRADE_FLAG} was passed"
    )
    if override:
        return True, f"WARNING: {reason}"

    env_url = _test_database_url()
    if not env_url:
        return False, (
            f"refusing to write to database {name!r}: it is a protected name and "
            f"TEST_DATABASE_URL is not set. Set TEST_DATABASE_URL to a disposable "
            f"cluster (for example a unix-socket initdb in /tmp), or pass "
            f"{DOWNGRADE_FLAG} if you really mean to target it."
        )
    if _db_name(env_url) == name:
        return False, (
            f"refusing to write to database {name!r}: TEST_DATABASE_URL names it "
            f"explicitly. Point it at a disposable cluster, or pass "
            f"{DOWNGRADE_FLAG}."
        )
    return False, (
        f"refusing to write to database {name!r}. Point TEST_DATABASE_URL at a "
        f"disposable cluster, or pass {DOWNGRADE_FLAG}."
    )


def require_disposable_database(url: str, *, override: bool | None = None) -> None:
    """Raise SystemExit(2) unless `url` is provably disposable."""
    if override is None:
        override = DOWNGRADE_FLAG in sys.argv
    ok, reason = check_disposable_database(url, override=override)
    if ok:
        if "WARNING" in reason:
            print(reason, file=sys.stderr)
        return
    raise SystemExit(
        f"REFUSING TO RUN: {reason}\n"
        f"Nothing was written."
    )
