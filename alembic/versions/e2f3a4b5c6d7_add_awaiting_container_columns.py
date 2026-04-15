"""Add columns for out-of-process container test dispatch.

Async runners (e.g. GitHub Actions) set external_run_url + callback_token +
dispatch_context when they hand off a test. The callback endpoint reads
these to verify the request and finalize the submission.

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-04-14
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e2f3a4b5c6d7"
down_revision: Union[str, None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("submissions", sa.Column("external_run_url", sa.String(512), nullable=True))
    op.add_column("submissions", sa.Column("callback_token", sa.String(64), nullable=True))
    op.add_column("submissions", sa.Column("dispatch_context", sa.JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("submissions", "dispatch_context")
    op.drop_column("submissions", "callback_token")
    op.drop_column("submissions", "external_run_url")
