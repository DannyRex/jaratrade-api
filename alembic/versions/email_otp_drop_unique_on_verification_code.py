"""drop unique on email_verification_tokens.code

Revision ID: email_otp_drop_unique
Revises: free_plan_one_store
Create Date: 2026-05-23 16:00:00.000000

Email verification switched from a 24-byte URL-safe token to a 6-digit OTP
because long opaque tokens get line-wrapped/rewritten by email clients (the
"link expired but pasting works" report) and aren't a friendly mobile UX
to paste. Lookup is now scoped by (user_id, code) so codes can repeat
across users; that requires dropping the global UNIQUE on `code`.

Long tokens already in the table at migration time are left alone - the
verify endpoint still matches by (user_id, code) for them, so they keep
working until they expire and get cron-cleaned.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "email_otp_drop_unique"
down_revision: Union[str, Sequence[str], None] = "free_plan_one_store"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLAlchemy's UNIQUE + index=True creates a UNIQUE INDEX named after the
    # column. We replace it with a plain (non-unique) index so lookup by code
    # is still fast.
    with op.batch_alter_table("email_verification_tokens") as batch:
        batch.drop_index("ix_email_verification_tokens_code")
        batch.create_index("ix_email_verification_tokens_code", ["code"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("email_verification_tokens") as batch:
        batch.drop_index("ix_email_verification_tokens_code")
        batch.create_index("ix_email_verification_tokens_code", ["code"], unique=True)
