"""DATABASE_URL driver pinning.

SQLAlchemy 2.1 made psycopg (v3) the default driver for postgresql://, and only
psycopg2 is installed, so a bare URL failed at import ("No module named 'psycopg'").
"""

import pytest

from app.config import Settings


@pytest.mark.parametrize(
    "given, want",
    [
        ("postgresql://u:p@h:5432/db", "postgresql+psycopg2://u:p@h:5432/db"),
        ("postgres://u:p@h:5432/db", "postgresql+psycopg2://u:p@h:5432/db"),
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("sqlite:///x.db", "sqlite:///x.db"),
    ],
)
def test_database_url_pins_psycopg2(given, want, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", given)
    assert Settings(_env_file=None).database_url == want


def test_default_url_pins_psycopg2(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert Settings(_env_file=None).database_url.startswith("postgresql+psycopg2://")
