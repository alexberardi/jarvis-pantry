"""Database session management."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from .config import get_settings


def _driver_url(url: str) -> str:
    """SQLAlchemy 2.1 maps a bare ``postgresql://`` URL to psycopg v3; this project installs psycopg2."""
    if url.startswith("postgresql://"):
        return "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


engine = create_engine(
    _driver_url(get_settings().database_url),
    pool_pre_ping=True,
    pool_recycle=600,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency for database sessions."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
