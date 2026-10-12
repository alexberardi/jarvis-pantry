"""Mark the local (private packages) author with is_local and move it off a username a GitHub login could take.

The local author used to be recognised by ``github_username == "local"``, which a GitHub account named ``local``
could claim through the login fallback that matches by username. It now carries ``is_local`` and the username
``_local`` (GitHub logins cannot start with an underscore).

Revision ID: j7f1a3b5c8d9
Revises: i6e0f2a4b7c8
Create Date: 2026-10-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "j7f1a3b5c8d9"
down_revision: Union[str, None] = "i6e0f2a4b7c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "authors",
        sa.Column("is_local", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.execute(
        "UPDATE authors SET is_local = true, github_username = '_local' "
        "WHERE github_id = -1 AND github_username = 'local'"
    )


def downgrade() -> None:
    op.execute("UPDATE authors SET github_username = 'local' WHERE is_local = true AND github_username = '_local'")
    op.drop_column("authors", "is_local")
