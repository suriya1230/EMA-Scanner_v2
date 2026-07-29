"""Add ema_fast/mid/slow and score columns to imported_signals

Revision ID: 007
Revises: 006
Create Date: 2026-07-23
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("imported_signals", sa.Column("ema_fast", sa.Float(), nullable=True))
    op.add_column("imported_signals", sa.Column("ema_mid", sa.Float(), nullable=True))
    op.add_column("imported_signals", sa.Column("ema_slow", sa.Float(), nullable=True))
    op.add_column("imported_signals", sa.Column("score", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("imported_signals", "score")
    op.drop_column("imported_signals", "ema_slow")
    op.drop_column("imported_signals", "ema_mid")
    op.drop_column("imported_signals", "ema_fast")
