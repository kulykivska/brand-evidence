"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-14
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "posts",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("platform", sa.Text, nullable=False),
        sa.Column("external_id", sa.Text),
        sa.Column("url", sa.Text, nullable=False, unique=True),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("language", sa.Text, nullable=False),
        sa.Column("published_at", sa.Text, nullable=False),
        sa.Column("ingested_at", sa.Text, nullable=False),
        sa.Column("brand_mentioned", sa.Boolean, nullable=False),
        sa.Column("source_path", sa.Text, nullable=False),
        sa.UniqueConstraint("platform", "external_id", name="uq_posts_platform_ext"),
    )
    op.create_table(
        "mentions",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("url", sa.Text, nullable=False, unique=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("excerpt", sa.Text, nullable=False),
        sa.Column("author", sa.Text),
        sa.Column("published_at", sa.Text),
        sa.Column("discovered_at", sa.Text, nullable=False),
        sa.Column("matched_terms", sa.JSON, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("notes", sa.Text),
    )
    op.create_table(
        "artifacts",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("subject_type", sa.Text, nullable=False),
        sa.Column("subject_id", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("sha256", sa.Text, nullable=False),
        sa.Column("bytes", sa.Integer, nullable=False),
        sa.Column("captured_at", sa.Text, nullable=False),
        sa.Column("capture_meta", sa.JSON, nullable=False),
    )
    op.create_index("ix_artifacts_subject", "artifacts", ["subject_type", "subject_id"])
    op.create_table(
        "archive_snapshots",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("subject_type", sa.Text, nullable=False),
        sa.Column("subject_id", sa.Text, nullable=False),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("request_url", sa.Text, nullable=False),
        sa.Column("provider_ref", sa.Text),
        sa.Column("snapshot_url", sa.Text),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("submitted_at", sa.Text, nullable=False),
        sa.Column("confirmed_at", sa.Text),
        sa.Column("attempts", sa.Integer, nullable=False),
    )
    op.create_index("ix_archive_status", "archive_snapshots", ["status"])
    op.create_table(
        "evidence_log",
        sa.Column("seq", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("occurred_at", sa.Text, nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("prev_hash", sa.Text, nullable=False),
        sa.Column("entry_hash", sa.Text, nullable=False, unique=True),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("job", sa.Text, nullable=False),
        sa.Column("started_at", sa.Text, nullable=False),
        sa.Column("finished_at", sa.Text),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("stats", sa.JSON, nullable=False),
        sa.Column("error", sa.Text),
    )
    op.create_index("ix_runs_job_started", "runs", ["job", "started_at"])
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS evidence_log_no_update BEFORE UPDATE ON evidence_log "
        "BEGIN SELECT RAISE(ABORT, 'evidence_log is append-only'); END;"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS evidence_log_no_delete BEFORE DELETE ON evidence_log "
        "BEGIN SELECT RAISE(ABORT, 'evidence_log is append-only'); END;"
    )


def downgrade() -> None:
    # The evidence log is never dropped by a migration. Downgrade is intentionally unsupported.
    raise RuntimeError("downgrade would destroy the evidence record; refused")
