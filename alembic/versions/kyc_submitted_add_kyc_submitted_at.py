"""add kyc_submitted_at to users

Revision ID: kyc_submitted
Revises: confirm_receipt
Create Date: 2026-05-20 07:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'kyc_submitted'
down_revision: Union[str, Sequence[str], None] = 'confirm_receipt'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add users.kyc_submitted_at.

    NULL  = exporter signed up but hasn't pressed "Submit for review" yet
            (profile incomplete / in progress).
    set   = exporter submitted; the admin KYC queue picks them up.

    Backfill: any exporter already in a non-"pending" state (approved or
    rejected) clearly went through review at some point, so we stamp their
    kyc_submitted_at with kyc_reviewed_at (falling back to time_created) so
    historical rows aren't left in a weird "approved but never submitted"
    shape. Pending exporters stay NULL - they'll show as Incomplete until
    they submit, which is correct.
    """
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("kyc_submitted_at", sa.DateTime(), nullable=True))

    conn = op.get_bind()
    conn.execute(sa.text("""
        UPDATE users
        SET kyc_submitted_at = COALESCE(kyc_reviewed_at, time_created)
        WHERE role = 'exporter'
          AND kyc_status IN ('approved', 'rejected')
          AND kyc_submitted_at IS NULL
    """))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("kyc_submitted_at")
