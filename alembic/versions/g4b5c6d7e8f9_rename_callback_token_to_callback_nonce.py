"""Rename submissions.callback_token to submissions.callback_nonce (#25).

The per-submission value mixed into the HMAC is no longer a secret token — it's
a public nonce that pairs with the server-held signing key. Column rename only
(no data shape change), so in-flight awaiting_container rows keep their value.

Revision ID: g4b5c6d7e8f9
Revises: f3a4b5c6d7e8
Create Date: 2026-05-19
"""
from typing import Sequence, Union

from alembic import op


revision: str = "g4b5c6d7e8f9"
down_revision: Union[str, None] = "f3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("submissions", "callback_token", new_column_name="callback_nonce")


def downgrade() -> None:
    op.alter_column("submissions", "callback_nonce", new_column_name="callback_token")
