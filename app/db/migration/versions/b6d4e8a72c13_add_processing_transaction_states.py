"""Add transaction processing and review states.

Revision ID: b6d4e8a72c13
Revises: 032f2bef8d8d
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b6d4e8a72c13"
down_revision: Union[str, None] = "032f2bef8d8d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

old_status = sa.Enum("pending", "completed", "canceled", "refunded", name="transactionstatus")
new_status = sa.Enum(
    "pending", "processing", "completed", "review_required", "canceled", "refunded",
    name="transactionstatus",
)


def upgrade() -> None:
    # Alembic's batch operation copies existing SQLite rows while widening VARCHAR.
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.alter_column(
            "status", existing_type=old_status, type_=new_status, existing_nullable=False
        )


def downgrade() -> None:
    bind = op.get_bind()
    active = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM transactions "
            "WHERE status IN ('processing', 'review_required')"
        )
    ).scalar_one()
    if active:
        raise RuntimeError("Resolve processing/review_required transactions before downgrade")
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.alter_column(
            "status", existing_type=new_status, type_=old_status, existing_nullable=False
        )
