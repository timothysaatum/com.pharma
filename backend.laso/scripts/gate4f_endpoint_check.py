#!/usr/bin/env python3
"""
GATE 4f — endpoint checks against atlasdb, READ ONLY.

The running container `kratos_backend` is the OLD image (its /app files predate
this work and it has no `server_head_seq`), so it cannot answer for the new
code. Per the fallback in the task, this drives the REAL FastAPI app in-process
with TestClient instead.

READ-ONLY is enforced by PostgreSQL, not by this script: every pooled connection
sets default_transaction_read_only=on, so any write attempted through the app
would be rejected by the server.

Endpoints:
  GET /api/v1/drugs?page=1&page_size=20&branch_id=<branch>
  GET /api/v1/sync/events?after_seq=69&limit=200
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

BRANCH = "72b2433d-120b-42a2-918b-e6dfcf176b1a"
AUTHOR = "bae475d9-994a-4d5b-abb2-32aa4b082602"


def main() -> int:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    import main as app_main
    from app.core.deps import get_current_active_user, get_current_user
    from app.db.dependencies import get_db
    from app.models.user.user_model import User

    url = os.environ.get("DATABASE_URL") or ""
    # Read-only is set SERVER-SIDE for every connection on this engine, so a
    # write attempted anywhere through the app is rejected by PostgreSQL rather
    # than prevented by this script. asyncpg takes it as a server setting; the
    # sync `connect` event does not work here because the cursor is async-adapted.
    engine = create_async_engine(
        url,
        future=True,
        connect_args={"server_settings": {"default_transaction_read_only": "on"}},
    )

    async def ro_session():
        async with AsyncSession(bind=engine, expire_on_commit=False) as s:
            yield s

    app = app_main.app
    app.dependency_overrides[get_db] = ro_session

    import asyncio

    from httpx import ASGITransport, AsyncClient

    async def run():
        # Resolve the real user from atlasdb and stand in for auth.
        async with AsyncSession(bind=engine, expire_on_commit=False) as s:
            from sqlalchemy import text

            row = (
                await s.execute(
                    text(
                        "SELECT id, username, email, full_name, organization_id, "
                        "is_super_admin, is_active, password_hash "
                        "FROM users WHERE id = :i"
                    ),
                    {"i": AUTHOR},
                )
            ).mappings().first()
            if row is None:
                print("FATAL: author user not found in atlasdb")
                return 1
            user = User(
                id=row["id"],
                username=row["username"],
                email=row["email"],
                full_name=row["full_name"],
                organization_id=row["organization_id"],
                is_super_admin=row["is_super_admin"],
                is_active=row["is_active"],
                password_hash=row["password_hash"],
                assigned_branches=[],
            )
            # assigned_branches is a UUID ARRAY; the drug endpoint refuses a
            # branch the user is not assigned to. Read the real assignment.
            _raw_branches = (
                await s.execute(
                    text("SELECT assigned_branches FROM users WHERE id = :u"),
                    {"u": AUTHOR},
                )
            ).scalar_one()
            # Read through raw text() the UUID ARRAY comes back as its JSON text,
            # so it must be decoded; list() on it yields CHARACTERS.
            import json as _json

            _branches = (
                _json.loads(_raw_branches)
                if isinstance(_raw_branches, str)
                else list(_raw_branches or [])
            )
            user.assigned_branches = [uuid.UUID(str(b)) for b in _branches]
            # tim is NOT a super admin; has_permission() short-circuits on
            # is_super_admin and otherwise reads user.roles. Without the roles
            # loaded the drugs endpoint answers 403 rather than 200. Read the
            # real grant rather than forcing the check, so this exercises the
            # genuine permission path.
            role_rows = (
                await s.execute(
                    text(
                        "SELECT r.id, r.name, r.permissions FROM roles r "
                        "JOIN user_roles ur ON ur.role_id = r.id "
                        "WHERE ur.user_id = :u"
                    ),
                    {"u": AUTHOR},
                )
            ).mappings().all()
            from app.models.user.user_model import Role

            def _perms(v):
                """roles.permissions is a TEXT column holding a JSON array, so it
                arrives as a string. list() on it splits into CHARACTERS, which
                silently made has_permission() return False."""
                import json as _json

                if v is None:
                    return []
                if isinstance(v, str):
                    return _json.loads(v)
                return list(v)

            user.roles = [
                Role(id=r["id"], name=r["name"], permissions=_perms(r["permissions"]))
                for r in role_rows
            ]
            print(
                f"acting as {user.username}: super_admin={user.is_super_admin} "
                f"roles={[r.name for r in user.roles]} "
                f"assigned_branches={[str(b) for b in user.assigned_branches]}"
            )
            head = (
                await s.execute(
                    text(
                        "SELECT coalesce(max(seq),0) FROM event_log "
                        "WHERE org_id = :o"
                    ),
                    {"o": str(row["organization_id"])},
                )
            ).scalar_one()

        # The drugs endpoint guards with require_permission("view_drugs"), whose
        # inner dependency is get_current_user — a DIFFERENT function object from
        # get_current_active_user, so overriding only the latter leaves the route
        # unauthenticated (401). Override both. `tim` is a super admin, so
        # has_permission passes.
        app.dependency_overrides[get_current_active_user] = lambda: user
        app.dependency_overrides[get_current_user] = lambda: user

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            r1 = await c.get(
                "/api/v1/drugs",
                params={"page": 1, "page_size": 20, "branch_id": BRANCH},
            )
            print(f"GET /api/v1/drugs?page=1&page_size=20&branch_id={BRANCH}")
            print(f"  status: {r1.status_code}")
            if r1.status_code == 200:
                body = r1.json()
                items = body.get("items") or body.get("data") or []
                print(f"  drugs returned: {len(items)}")
                for d in items:
                    print(
                        f"    - {d.get('name'):<22} reorder_quantity="
                        f"{d.get('reorder_quantity')}"
                    )
                names = sorted(d.get("name") for d in items)
                print(f"  names: {json.dumps(names)}")
                print(f"  EXPECT 6 drugs: {'YES' if len(items) == 6 else 'NO'}")
            else:
                print(f"  body: {r1.text[:600]}")

            r2 = await c.get(
                "/api/v1/sync/events",
                params={"after_seq": 69, "limit": 200},
            )
            print()
            print("GET /api/v1/sync/events?after_seq=69&limit=200")
            print(f"  status: {r2.status_code}")
            if r2.status_code == 200:
                b2 = r2.json()
                print(f"  events returned: {len(b2.get('events', []))}")
                print(f"  next_after_seq:  {b2.get('next_after_seq')}")
                print(f"  server_head_seq: {b2.get('server_head_seq')}")
                print(f"  atlasdb head:    {head}")
                ok = b2.get("server_head_seq") == head
                print(f"  server_head_seq == atlasdb max(seq): {'YES' if ok else 'NO'}")
            else:
                print(f"  body: {r2.text[:600]}")
        await engine.dispose()
        return 0

    try:
        return asyncio.run(run())
    finally:
        app.dependency_overrides.clear()


if __name__ == "__main__":
    sys.exit(main())
