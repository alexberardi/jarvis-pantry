"""Add batch_id column to submissions for operator bulk uploads.

Groups submissions enqueued together by the operator bulk endpoint
(POST /v1/admin/bulk-submissions) so the status endpoint can fetch them
as a set. Single submissions leave this NULL.

Revision ID: h5d9e1f3a6b7
Revises: g4b5c6d7e8f9
Create Date: 2026-06-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "h5d9e1f3a6b7"
down_revision: Union[str, None] = "g4b5c6d7e8f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("submissions", sa.Column("batch_id", sa.String(length=36), nullable=True))
    op.create_index("ix_submissions_batch_id", "submissions", ["batch_id"])


def downgrade() -> None:
    op.drop_index("ix_submissions_batch_id", table_name="submissions")
    op.drop_column("submissions", "batch_id")
