"""Add app_settings key/value table.

Holds settings that are changed at run time rather than by environment variable, starting with the Forge's local
OpenAI-compatible LLM server (base URL, API key, default model).

Revision ID: i6e0f2a4b7c8
Revises: h5d9e1f3a6b7
Create Date: 2026-10-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "i6e0f2a4b7c8"
down_revision: Union[str, None] = "h5d9e1f3a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
