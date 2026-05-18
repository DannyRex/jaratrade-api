"""backfill demo exporter banking info

Revision ID: backfill_exp_bank
Revises: flw_subaccount
Create Date: 2026-05-18 17:49:45.027347

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'backfill_exp_bank'
down_revision: Union[str, Sequence[str], None] = 'flw_subaccount'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Backfill banking info for any approved exporter that doesn't have
    bank_id + account_number on their business profile.

    The seed adds these on fresh DBs but historical prod records pre-date
    those fields. Without them /adm/payouts/eligible never returns anything
    and /adm/users/{id}/reprovision-subaccount errors out. This migration:

      1. Picks any one Nigerian bank that has a flutter_code (idempotent).
      2. Sets bank_id + account_number + account_name on each affected
         BusinessProfile row, leaving rows that already have data alone.

    The placeholder account_number "0123456789" is the Flutterwave test-mode
    NUBAN that resolves cleanly against their /v3/accounts/resolve in test
    keys. Replace once a real bank account is on file.
    """
    conn = op.get_bind()
    # Find a bank to anchor on
    bank_row = conn.execute(sa.text(
        "SELECT id FROM banks WHERE flutter_code IS NOT NULL AND status = 1 LIMIT 1"
    )).fetchone()
    if not bank_row:
        return  # No banks seeded yet; nothing to do.
    bank_id = bank_row[0]

    # Update only rows that are missing the data, so re-running is a no-op.
    conn.execute(sa.text("""
        UPDATE business_profiles
        SET bank_id = :bank_id,
            account_number = COALESCE(account_number, '0123456789'),
            account_name = COALESCE(account_name, business_name)
        WHERE bank_id IS NULL OR account_number IS NULL
    """), {"bank_id": bank_id})


def downgrade() -> None:
    """Not reversible - we don't know which rows we updated. No-op."""
    pass
