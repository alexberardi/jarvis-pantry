"""Add resolved_lockfile column to submissions (#21).

Stores the frozen `uv pip compile` output produced at submission acceptance.
The runner installs from this string verbatim instead of resolving packages
at test time.

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-05-18
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f3a4b5c6d7e8"
down_revision: Union[str, None] = "e2f3a4b5c6d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("submissions", sa.Column("resolved_lockfile", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("submissions", "resolved_lockfile")
