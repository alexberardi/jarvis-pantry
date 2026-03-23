"""add validation pipeline columns

Revision ID: a3f2b8c91d4e
Revises: 6e81858e3cb2
Create Date: 2026-03-18 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a3f2b8c91d4e"
down_revision: Union[str, None] = "6e81858e3cb2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("submissions", sa.Column("static_analysis_result", sa.JSON(), nullable=True))
    op.add_column("submissions", sa.Column("container_test_result", sa.JSON(), nullable=True))
    op.add_column("submissions", sa.Column("llm_provider", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("submissions", "llm_provider")
    op.drop_column("submissions", "container_test_result")
    op.drop_column("submissions", "static_analysis_result")
