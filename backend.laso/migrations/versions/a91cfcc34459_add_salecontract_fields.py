"""add salecontract fields

Revision ID: a91cfcc34459
Revises: e9f1a2b3c4d5
Create Date: 2026-06-26 18:30:25.699134

"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = 'a91cfcc34459'
down_revision: Union[str, None] = 'e9f1a2b3c4d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # These fields and their constraints were already added by revision
    # e9f1a2b3c4d5. This historical duplicate revision must remain in the graph,
    # but performing the generated operations makes every fresh deployment fail.
    pass


def downgrade() -> None:
    pass
