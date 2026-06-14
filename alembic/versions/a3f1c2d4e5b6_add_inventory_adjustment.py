"""add inventory_adjustment table

Revision ID: a3f1c2d4e5b6
Revises: 6a97164e3d36
Create Date: 2026-06-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a3f1c2d4e5b6"
down_revision: Union[str, None] = "6a97164e3d36"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "inventory_adjustment",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "inventory_unit_id",
            sa.Integer(),
            sa.ForeignKey("inventory_unit.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )


def downgrade() -> None:
    op.drop_table("inventory_adjustment")
