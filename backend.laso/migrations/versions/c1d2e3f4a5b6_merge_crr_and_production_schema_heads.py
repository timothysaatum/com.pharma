"""merge CRR and production schema heads

Revision ID: c1d2e3f4a5b6
Revises: 8a9b0c1d2e3f, fa1b2c3d4e5f
Create Date: 2026-07-11 12:15:00.000000

This is intentionally a no-op merge. Both parent migrations contain their own
schema changes; joining their revision ancestry restores a single canonical
``head`` for deployment tooling.
"""

from collections.abc import Sequence


revision: str = "c1d2e3f4a5b6"
down_revision: tuple[str, str] = ("8a9b0c1d2e3f", "fa1b2c3d4e5f")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
