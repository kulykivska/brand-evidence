"""SQLAlchemy models. Column semantics follow requirements §5."""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Boolean, Integer, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[str]: JSON}

    def as_payload(self) -> dict[str, Any]:
        """Full row as a JSON-safe dict, used as the evidence_log payload."""
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (UniqueConstraint("platform", "external_id", name="uq_posts_platform_ext"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[str] = mapped_column(Text, nullable=False)
    ingested_at: Mapped[str] = mapped_column(Text, nullable=False)
    brand_mentioned: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)


class Mention(Base):
    __tablename__ = "mentions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    excerpt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    author: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[str | None] = mapped_column(Text)
    discovered_at: Mapped[str] = mapped_column(Text, nullable=False)
    matched_terms: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="new")
    notes: Mapped[str | None] = mapped_column(Text)
    # Capture is retried across runs: a mention the budget or a failure skipped
    # used to stay uncaptured forever. States: ingest.crawler.CAPTURE_STATES.
    capture_state: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    capture_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    subject_type: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    captured_at: Mapped[str] = mapped_column(Text, nullable=False)
    capture_meta: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ArchiveSnapshot(Base):
    __tablename__ = "archive_snapshots"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    subject_type: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    request_url: Mapped[str] = mapped_column(Text, nullable=False)
    provider_ref: Mapped[str | None] = mapped_column(Text)
    snapshot_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    submitted_at: Mapped[str] = mapped_column(Text, nullable=False)
    confirmed_at: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class EvidenceEntry(Base):
    __tablename__ = "evidence_log"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # Unique: one successor per entry, so a forked chain cannot be written.
    prev_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    entry_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)


class ChainCheckpoint(Base):
    """The highest seq whose chain has been verified in full, and when.

    Routine readers (digest, report) re-check only what came after the last
    checkpoint; `verify` and `export` still walk the whole chain.
    """

    __tablename__ = "chain_checkpoints"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    # Unique: two checkpoints at one seq make the trusted one arbitrary.
    seq: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    entry_hash: Mapped[str] = mapped_column(Text, nullable=False)
    verified_at: Mapped[str] = mapped_column(Text, nullable=False)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    job: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[str] = mapped_column(Text, nullable=False)
    finished_at: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="running")
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
