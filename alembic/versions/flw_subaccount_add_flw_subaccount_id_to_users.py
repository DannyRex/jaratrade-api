"""add flw_subaccount_id to users

Revision ID: flw_subaccount
Revises: backfill_stock
Create Date: 2026-05-18 17:21:47.944698

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'flw_subaccount'
down_revision: Union[str, Sequence[str], None] = 'backfill_stock'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """v3.5: Flutterwave subaccount columns on users + new payouts table.

    `users.flw_subaccount_id`      - the public subaccount ID returned by FLW
                                     (POST /v3/subaccounts). Used in payment splits.
    `users.flw_subaccount_payload` - raw JSON response audit trail.
    `payouts` table                - per-order seller payout records with FLW
                                     transfer reference + status lifecycle.
    """
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("flw_subaccount_id", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("flw_subaccount_payload", sa.Text(), nullable=True))

    op.create_table(
        "payouts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("seller_id", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="NGN"),
        sa.Column("reference", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("provider", sa.String(length=40), nullable=False, server_default="flutterwave"),
        sa.Column("provider_payload", sa.Text(), nullable=True),
        sa.Column("initiated_by", sa.String(length=64), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("time_created", sa.DateTime(), nullable=False),
        sa.Column("time_updated", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("reference"),
    )
    with op.batch_alter_table("payouts", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_payouts_order_id"), ["order_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_payouts_seller_id"), ["seller_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_payouts_reference"), ["reference"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("payouts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_payouts_reference"))
        batch_op.drop_index(batch_op.f("ix_payouts_seller_id"))
        batch_op.drop_index(batch_op.f("ix_payouts_order_id"))
    op.drop_table("payouts")

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("flw_subaccount_payload")
        batch_op.drop_column("flw_subaccount_id")
