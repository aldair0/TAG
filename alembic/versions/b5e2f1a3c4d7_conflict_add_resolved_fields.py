"""conflict: add resolved_by and resolved_notes

Revision ID: b5e2f1a3c4d7
Revises: a3f1c2d4e5b6
Create Date: 2026-06-14

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "b5e2f1a3c4d7"
down_revision = "a3f1c2d4e5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conflict", sa.Column("resolved_by", sa.String(64), nullable=True))
    op.add_column("conflict", sa.Column("resolved_notes", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("conflict", "resolved_notes")
    op.drop_column("conflict", "resolved_by")
