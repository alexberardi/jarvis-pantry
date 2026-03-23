"""Add forge_drafts table for share-code test installs.

Revision ID: c4d5e6f7g8h9
Revises: b5c7d9e1f3a6
Create Date: 2026-03-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4d5e6f7g8h9"
down_revision: Union[str, None] = "b5c7d9e1f3a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "forge_drafts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("share_code", sa.String(6), unique=True, nullable=False, index=True),
        sa.Column("session_id", sa.String(36), nullable=False, index=True),
        sa.Column("package_name", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("files_json", sa.JSON, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("forge_drafts")
