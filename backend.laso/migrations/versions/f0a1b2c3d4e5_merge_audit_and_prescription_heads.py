"""merge audit sync repair and prescription branch heads

Revision ID: f0a1b2c3d4e5
Revises: 15a1c14d1b9b, e3f4a5b6c7d8
Create Date: 2026-07-12 14:45:00.000000

This is a no-op merge that restores a single Alembic head after the local
prescription branch migration diverged from the audit-log sync repair branch.
"""

from collections.abc import Sequence


revision: str = "f0a1b2c3d4e5"
down_revision: tuple[str, str] = ("15a1c14d1b9b", "e3f4a5b6c7d8")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
