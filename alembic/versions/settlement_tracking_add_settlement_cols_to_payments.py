"""add settlement tracking columns to payments

Revision ID: settlement_tracking
Revises: email_otp_drop_unique
Create Date: 2026-05-24 22:55:00.000000

International (non-NGN) collections via Flutterwave settle T+5 business
days, meaning the buyer's payment doesn't reach our NGN wallet until 5
working days after the charge. Our existing payout cron releases seller
funds 1 day after delivery confirmation - far inside that 5-day window.
Without gating, a UK-funded payout would either fail with insufficient
balance OR draw against unrelated NGN collections we'd later need to
reconcile.

These three columns let us track per-payment settlement state from FLW
so the payout cron can require settlement_status='completed' before
dispatching. Domestic NGN collections leave them null (they settle T+1
which is implicitly inside our dispute window, so no gating needed).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "settlement_tracking"
down_revision: Union[str, Sequence[str], None] = "email_otp_drop_unique"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("payments") as batch:
        batch.add_column(sa.Column("flw_settlement_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("settlement_due_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("settlement_status", sa.String(20), nullable=True))
        batch.create_index(
            "ix_payments_flw_settlement_id",
            ["flw_settlement_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("payments") as batch:
        batch.drop_index("ix_payments_flw_settlement_id")
        batch.drop_column("settlement_status")
        batch.drop_column("settlement_due_at")
        batch.drop_column("flw_settlement_id")
