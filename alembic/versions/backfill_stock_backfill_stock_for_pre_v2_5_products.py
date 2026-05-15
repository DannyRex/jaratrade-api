"""backfill stock for pre-v2.5 products

Revision ID: backfill_stock
Revises: 27cc167e01b9
Create Date: 2026-05-15 00:13:30.677768

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'backfill_stock'
down_revision: Union[str, Sequence[str], None] = '27cc167e01b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Backfill stock for products that pre-date v2.5.

    The previous migration added `stock_quantity INTEGER NOT NULL DEFAULT 0` -
    correct for new rows, but on prod it zeroed out every existing listing,
    making the whole marketplace appear out-of-stock until each seller hand-
    sets it. Heuristic: any product whose stock is 0 AND whose
    last_inventory_update_at is still NULL came from the v2.5 backfill, not
    from a seller's deliberate "sold out" action. Set those to 50 (a
    conservative starting count) and stamp the timestamp so re-running this
    migration after a seller has set their own values is a no-op.
    """
    op.execute(sa.text(
        "UPDATE products "
        "SET stock_quantity = 50, "
        "    last_inventory_update_at = CURRENT_TIMESTAMP "
        "WHERE stock_quantity = 0 AND last_inventory_update_at IS NULL"
    ))


def downgrade() -> None:
    """Not reversible - we lose the prior zero. No-op."""
    pass
