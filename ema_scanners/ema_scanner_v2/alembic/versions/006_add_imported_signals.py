"""Add imported_signals table — for the CSV Backtest upload feature

Revision ID: 006
Revises: 005
Create Date: 2026-07-14
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "imported_signals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("market", sa.String(length=10), nullable=False, server_default="futures"),
        sa.Column("interval", sa.String(length=5), nullable=False, server_default="1h"),
        sa.Column("signal_type", sa.String(length=4), nullable=False),
        sa.Column("cross_price", sa.Float(), nullable=True),
        sa.Column("cross_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_imported_signals_symbol", "imported_signals", ["symbol"])


def downgrade() -> None:
    op.drop_index("ix_imported_signals_symbol", table_name="imported_signals")
    op.drop_table("imported_signals")
