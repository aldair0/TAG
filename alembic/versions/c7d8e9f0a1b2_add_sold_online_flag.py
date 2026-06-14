"""Add sold_online_at / sold_online_until to inventory_unit.

Revision ID: c7d8e9f0a1b2
Revises: b5e2f1a3c4d7
Create Date: 2026-06-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c7d8e9f0a1b2"
down_revision = "b5e2f1a3c4d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("inventory_unit", sa.Column("sold_online_at", sa.DateTime(), nullable=True))
    op.add_column("inventory_unit", sa.Column("sold_online_until", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("inventory_unit", "sold_online_until")
    op.drop_column("inventory_unit", "sold_online_at")
