"""tighten free tier to 1 store

Revision ID: free_plan_one_store
Revises: kyc_submitted
Create Date: 2026-05-23 02:00:00.000000

Business call: a Nigerian seller's "store" and "market location" are the
same physical thing - one shop in one marketplace. Exposing both as
separate caps on the plan card was confusing ("up to 2 stores in 1 market
location") and the 2-stores-in-1-market case is functionally useless
because nobody operates two adjacent stalls. Tightening Free to 1 store
unifies the two concepts and gives the Premium upgrade a clean trigger
(any physical expansion).

This migration only updates the seeded Free Tier (is_default=1) row, and
ONLY if it's still at max_store=2 - so it's idempotent and won't clobber
an admin who manually adjusted the plan via /admin/plans. Existing Free
sellers who already have 2 stores are grandfathered: the create_store
guard uses `>=` so their 2 stores stay live; they just can't add a 3rd.

Premium plan is untouched.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "free_plan_one_store"
down_revision: Union[str, Sequence[str], None] = "kyc_submitted"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NB: keep the SQL string literal inline. SQLite does NOT support
    # SQL adjacent-string-literal concatenation, so splitting the
    # description across two quoted literals breaks CI even though
    # Postgres (prod) tolerates it.
    op.execute(
        sa.text(
            """
            UPDATE exporter_plans
            SET max_store = 1,
                description = '1 store and up to 5 product listings. 2% commission per transaction.'
            WHERE is_default = 1
              AND max_store = 2
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE exporter_plans
            SET max_store = 2,
                description = 'Up to 2 stores and 5 product listings. 2% commission per transaction.'
            WHERE is_default = 1
              AND max_store = 1
            """
        )
    )
