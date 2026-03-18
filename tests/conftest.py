"""Test fixtures for command store tests."""

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

from app.db import Base, get_db
from app.main import app
from app.models import Author, Command, CommandVersion


@pytest.fixture
def db_session(tmp_path):
    """Create a SQLite session for testing."""
    db_path = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def client(db_session):
    """FastAPI test client with overridden DB."""
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def seed_data(db_session):
    """Seed the database with sample data."""
    author = Author(
        id=1,
        github_id=12345,
        github_username="testuser",
        display_name="Test User",
        avatar_url="https://example.com/avatar.png",
    )
    db_session.add(author)

    cmd = Command(
        id=1,
        command_name="get_stock_price",
        display_name="Stock Price Checker",
        description="Get real-time stock prices by ticker symbol",
        github_repo_url="https://github.com/testuser/jarvis-command-stock-price",
        author_id=1,
        latest_version="1.0.0",
        categories=["finance", "information"],
        platforms=[],
        license="MIT",
        install_count=42,
        danger_rating=2,
        verified=True,
        published=True,
    )
    db_session.add(cmd)

    version = CommandVersion(
        id=1,
        command_id=1,
        version="1.0.0",
        git_tag="v1.0.0",
        manifest_json={"name": "get_stock_price", "version": "1.0.0"},
        danger_rating=2,
        min_jarvis_version="0.9.0",
    )
    db_session.add(version)

    cmd2 = Command(
        id=2,
        command_name="weather_alerts",
        display_name="Weather Alerts",
        description="Get severe weather alerts for your area",
        github_repo_url="https://github.com/testuser/jarvis-command-weather-alerts",
        author_id=1,
        latest_version="2.1.0",
        categories=["weather"],
        platforms=[],
        license="MIT",
        install_count=100,
        danger_rating=1,
        verified=False,
        published=True,
    )
    db_session.add(cmd2)

    db_session.commit()
    return {"author": author, "command": cmd, "command2": cmd2, "version": version}
