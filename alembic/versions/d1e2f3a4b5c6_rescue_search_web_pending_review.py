"""Rescue search_web submissions stuck in pending_review.

The old auto-publish gate required danger_score <= 3 from AI review, which
flagged legitimate network-using commands (like search_web) and left them
stuck. The gate is being relaxed to only honor a hard AI reject.

This migration is a one-off data rescue: the two duplicate search_web
submissions were the first real uses of the pipeline and never had a way
to reach `published`.

Revision ID: d1e2f3a4b5c6
Revises: c4d5e6f7g8h9
Create Date: 2026-04-14
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, None] = "c4d5e6f7g8h9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(sa.text("""
        DELETE FROM submissions
        WHERE id IN (
            SELECT s.id FROM submissions s
            JOIN commands c ON s.command_id = c.id
            WHERE c.command_name = 'search_web'
              AND s.status = 'pending_review'
            ORDER BY s.submitted_at ASC
            OFFSET 1
        )
    """))

    conn.execute(sa.text("""
        UPDATE submissions
        SET status = 'published',
            completed_at = COALESCE(completed_at, NOW())
        WHERE command_id IN (
            SELECT id FROM commands WHERE command_name = 'search_web'
        )
        AND status = 'pending_review'
    """))

    conn.execute(sa.text("""
        UPDATE commands
        SET published = true
        WHERE command_name = 'search_web' AND published = false
    """))


def downgrade() -> None:
    pass
