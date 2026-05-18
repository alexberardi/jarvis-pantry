"""Add awaiting_container_since + dispatch_attempts columns to submissions (#22).

Powers the callback-timeout retry watcher: every awaiting_container transition
stamps `awaiting_container_since` and bumps `dispatch_attempts`; the background
watcher reads (now - awaiting_container_since) against an exponential-backoff
threshold to decide retry-vs-fail.

Revision ID: a4b5c6d7e8f9
Revises: f3a4b5c6d7e8
Create Date: 2026-05-18
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a4b5c6d7e8f9"
down_revision: Union[str, None] = "f3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "submissions",
        sa.Column("awaiting_container_since", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "submissions",
        sa.Column(
            "dispatch_attempts",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("submissions", "dispatch_attempts")
    op.drop_column("submissions", "awaiting_container_since")
