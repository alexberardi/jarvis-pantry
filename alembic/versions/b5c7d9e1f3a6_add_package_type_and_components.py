"""add package_type and components to commands

Revision ID: b5c7d9e1f3a6
Revises: a3f2b8c91d4e
Create Date: 2026-03-18 18:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b5c7d9e1f3a6"
down_revision: Union[str, None] = "a3f2b8c91d4e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("commands", sa.Column("package_type", sa.String(20), server_default="command", nullable=True))
    op.add_column("commands", sa.Column("components", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("commands", "components")
    op.drop_column("commands", "package_type")
