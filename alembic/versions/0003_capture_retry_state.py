"""capture state on mentions, and the indexes every run filters on

A mention that was discovered but not captured - because the run hit its
budget, or the capture threw - was never retried: the crawl only captured
mentions it had just created. The evidence for it was silently lost.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-17
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# Filtered or ordered on by the digest, the report and the crawl, and unindexed
# until now: each was a full table scan on every scheduled run.
INDEXES = (
    ("ix_mentions_capture_state", "mentions", ["capture_state"]),
    ("ix_mentions_discovered_at", "mentions", ["discovered_at"]),
    ("ix_mentions_status", "mentions", ["status"]),
    ("ix_posts_ingested_at", "posts", ["ingested_at"]),
    ("ix_posts_source_path", "posts", ["source_path"]),
    ("ix_archive_snapshots_subject_id", "archive_snapshots", ["subject_id"]),
    ("ix_archive_snapshots_confirmed_at", "archive_snapshots", ["confirmed_at"]),
    ("ix_artifacts_kind", "artifacts", ["kind"]),
)


def upgrade() -> None:
    op.add_column(
        "mentions",
        sa.Column(
            "capture_state", sa.Text, nullable=False, server_default="pending"
        ),
    )
    op.add_column(
        "mentions",
        sa.Column("capture_attempts", sa.Integer, nullable=False, server_default="0"),
    )
    # Anything already captured has artifacts; anything else is genuinely
    # pending and will be picked up by the next crawl.
    op.execute(
        "UPDATE mentions SET capture_state = 'captured' WHERE id IN "
        "(SELECT subject_id FROM artifacts WHERE subject_type = 'mention')"
    )
    for name, table, columns in INDEXES:
        op.create_index(name, table, columns)


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported")
