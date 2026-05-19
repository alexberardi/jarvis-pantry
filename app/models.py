"""SQLAlchemy models for the command store."""

from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    ForeignKey,
    JSON,
    Text,
    UniqueConstraint,
)
from sqlalchemy import TypeDecorator
import json as _json


class StringArray(TypeDecorator):
    """Platform-agnostic array type: PostgreSQL ARRAY or JSON fallback."""

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
            return dialect.type_descriptor(PG_ARRAY(String))
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if dialect.name == "postgresql":
            return value
        return _json.dumps(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return []
        if dialect.name == "postgresql":
            return value
        return _json.loads(value)


class UtcDateTime(TypeDecorator):
    """DateTime that round-trips tz-aware UTC across PostgreSQL and SQLite.

    PostgreSQL's `TIMESTAMP WITH TIME ZONE` preserves tzinfo natively; SQLite
    silently strips it. Without this decorator, a row written tz-aware comes
    back tz-naive from SQLite — making `now - submission.awaiting_container_since`
    raise TypeError in the callback-timeout watcher (#22) under local/test runs
    even though prod (Postgres) is fine. This normalizes so both dialects
    return tz-aware UTC.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


from sqlalchemy.orm import relationship

from .db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Author(Base):
    __tablename__ = "authors"

    id = Column(Integer, primary_key=True)
    github_id = Column(Integer, unique=True, nullable=False)
    github_username = Column(String(255), unique=True, nullable=False)
    display_name = Column(String(255))
    avatar_url = Column(String(512))
    suspended = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    commands = relationship("Command", back_populates="author")
    reviews = relationship("Review", back_populates="author")


class Command(Base):
    __tablename__ = "commands"

    id = Column(Integer, primary_key=True)
    command_name = Column(String(255), unique=True, nullable=False, index=True)
    display_name = Column(String(255))
    description = Column(Text)
    github_repo_url = Column(String(512))
    author_id = Column(Integer, ForeignKey("authors.id"), nullable=False)
    latest_version = Column(String(50))
    categories = Column(StringArray(), default=list)
    platforms = Column(StringArray(), default=list)
    license = Column(String(100))
    icon_url = Column(String(512))
    install_count = Column(Integer, default=0)
    danger_rating = Column(Integer)  # 1-5
    package_type = Column(String(20), default="command")  # "command" or "bundle"
    components = Column(JSON, nullable=True)  # list of {type, name, path, description}
    verified = Column(Boolean, default=False)
    published = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    author = relationship("Author", back_populates="commands")
    versions = relationship("CommandVersion", back_populates="command", order_by="CommandVersion.published_at.desc()")
    reviews = relationship("Review", back_populates="command")


class CommandVersion(Base):
    __tablename__ = "command_versions"
    __table_args__ = (
        UniqueConstraint("command_id", "version", name="uq_command_version"),
    )

    id = Column(Integer, primary_key=True)
    command_id = Column(Integer, ForeignKey("commands.id"), nullable=False)
    version = Column(String(50), nullable=False)
    git_tag = Column(String(100))
    git_commit_sha = Column(String(40))
    manifest_json = Column(JSON)
    danger_rating = Column(Integer)
    security_report_id = Column(Integer, ForeignKey("security_reports.id"))
    min_jarvis_version = Column(String(50))
    published_at = Column(DateTime(timezone=True), default=_utcnow)

    command = relationship("Command", back_populates="versions")
    security_report = relationship("SecurityReport")


class SecurityReport(Base):
    __tablename__ = "security_reports"

    id = Column(Integer, primary_key=True)
    command_id = Column(Integer, ForeignKey("commands.id"), nullable=False)
    version = Column(String(50))
    ai_review_summary = Column(Text)
    ai_danger_score = Column(Integer)  # 1-5
    ai_concerns = Column(StringArray(), default=list)
    ai_recommendation = Column(String(20))  # approve, flag, reject
    overall_danger_rating = Column(Integer)
    raw_ai_response = Column(JSON)
    reviewed_at = Column(DateTime(timezone=True), default=_utcnow)


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("command_id", "author_id", name="uq_review_per_author"),
    )

    id = Column(Integer, primary_key=True)
    command_id = Column(Integer, ForeignKey("commands.id"), nullable=False)
    author_id = Column(Integer, ForeignKey("authors.id"), nullable=False)
    rating = Column(Integer, nullable=False)  # 1-5
    title = Column(String(255))
    body = Column(Text)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    command = relationship("Command", back_populates="reviews")
    author = relationship("Author", back_populates="reviews")


class ForgeDraft(Base):
    """Temporary draft from the Forge AI builder, addressable by share code.

    Share codes expire after 15 minutes but are refreshed on each new
    Forge message. Used for test-installing packages to nodes before publishing.
    """
    __tablename__ = "forge_drafts"

    id = Column(Integer, primary_key=True)
    share_code = Column(String(6), unique=True, nullable=False, index=True)
    session_id = Column(String(36), nullable=False, index=True)
    package_name = Column(String(255), nullable=False)
    display_name = Column(String(255), nullable=True)
    files_json = Column(JSON, nullable=False)  # [{filename, content, language}]
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class Submission(Base):
    __tablename__ = "submissions"

    id = Column(Integer, primary_key=True)
    command_id = Column(Integer, ForeignKey("commands.id"), nullable=True)
    github_repo_url = Column(String(512), nullable=False)
    author_id = Column(Integer, ForeignKey("authors.id"), nullable=False)
    status = Column(String(50), default="pending")  # pending, static_analysis, ai_review, container_test, awaiting_container, published, rejected, callback_timeout
    error_message = Column(Text)
    static_analysis_result = Column(JSON, nullable=True)
    container_test_result = Column(JSON, nullable=True)
    llm_provider = Column(String(20), nullable=True)
    submitted_at = Column(UtcDateTime(), default=_utcnow)
    completed_at = Column(UtcDateTime())
    # Async container-test dispatch: set when a runner kicks off an external test
    # (e.g. GitHub Actions). Callback uses callback_token to authenticate and
    # finalize the submission using dispatch_context.
    external_run_url = Column(String(512), nullable=True)
    callback_token = Column(String(64), nullable=True)
    dispatch_context = Column(JSON, nullable=True)
    # Set at every awaiting_container transition (initial dispatch + each watcher
    # retry). The callback-timeout watcher (#22) uses (now - this) to decide
    # whether a stalled row should be retried or marked callback_timeout.
    awaiting_container_since = Column(UtcDateTime(), nullable=True)
    dispatch_attempts = Column(Integer, default=0, nullable=False)
    # Frozen lockfile (`uv pip compile` output) resolved at submission acceptance.
    # The runner installs from this verbatim — no live PyPI resolution at run time.
    resolved_lockfile = Column(Text, nullable=True)
