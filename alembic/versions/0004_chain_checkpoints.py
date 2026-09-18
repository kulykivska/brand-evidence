"""checkpoints for chain verification

The digest and the report re-hashed every entry ever written, every day. That
is O(the whole log) on a routine path and it grows forever. A checkpoint marks
how far the chain has been verified in full; `verify` and `export` still walk
all of it.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-17
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chain_checkpoints",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("entry_hash", sa.Text(), nullable=False),
        sa.Column("verified_at", sa.Text(), nullable=False),
    )
    op.create_index("ix_chain_checkpoints_seq", "chain_checkpoints", ["seq"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_chain_checkpoints_seq", table_name="chain_checkpoints")
    op.drop_table("chain_checkpoints")
