"""add confirmed_received_at to orders

Revision ID: confirm_receipt
Revises: backfill_exp_bank
Create Date: 2026-05-18 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'confirm_receipt'
down_revision: Union[str, Sequence[str], None] = 'backfill_exp_bank'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the confirmed_received_at timestamp to orders.

    When the buyer explicitly confirms receipt of the order, we stamp this
    column. The payout dispatcher and the process_payouts cron treat any
    order with a non-null confirmed_received_at as immediately eligible
    for payout - the buyer has waived the 7-day dispute window.

    Nullable so historical rows stay untouched. Existing orders default
    to NULL meaning they still flow through the 7-day window as before.
    """
    with op.batch_alter_table("orders") as batch:
        batch.add_column(
            sa.Column("confirmed_received_at", sa.DateTime(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.drop_column("confirmed_received_at")
