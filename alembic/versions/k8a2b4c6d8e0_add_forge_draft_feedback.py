"""Add forge_drafts.feedback_json for Send to Forge reports.

Revision ID: k8a2b4c6d8e0
Revises: j7f1a3b5c8d9
Create Date: 2026-11-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "k8a2b4c6d8e0"
down_revision: Union[str, None] = "j7f1a3b5c8d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("forge_drafts", sa.Column("feedback_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("forge_drafts", "feedback_json")
