"""one successor per evidence entry

A unique index on prev_hash makes a forked chain unrepresentable: two entries
cannot both claim the same predecessor. Before this, two processes could each
read the head and each append after it, and the append-only triggers then made
the fork permanent.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-17
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    forks = conn.execute(
        sa.text(
            "SELECT prev_hash, COUNT(*) AS n FROM evidence_log "
            "GROUP BY prev_hash HAVING n > 1 ORDER BY n DESC"
        )
    ).all()
    if forks:
        # Refuse rather than drop rows: this table is the record, and which
        # branch is the real one is a question for a human.
        detail = ", ".join(f"{row[0][:12]}... x{row[1]}" for row in forks[:5])
        raise RuntimeError(
            "evidence_log already contains a forked chain, so the unique index "
            f"cannot be created: {detail}. Export the log and decide which "
            "branch to keep before upgrading; nothing here deletes evidence."
        )
    op.create_index(
        "ix_evidence_log_prev_hash_unique", "evidence_log", ["prev_hash"], unique=True
    )


def downgrade() -> None:
    # The evidence log is never loosened by a migration.
    raise NotImplementedError("downgrade is not supported for the evidence log")
