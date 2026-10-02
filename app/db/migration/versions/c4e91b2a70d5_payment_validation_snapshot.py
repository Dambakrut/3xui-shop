"""Persist immutable payment validation expectations.

Revision ID: c4e91b2a70d5
Revises: b6d4e8a72c13
"""
import sqlalchemy as sa
from alembic import op

revision = "c4e91b2a70d5"
down_revision = "b6d4e8a72c13"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("transactions") as batch:
        batch.add_column(sa.Column("payment_provider", sa.String(32), nullable=True))
        batch.add_column(sa.Column("expected_amount", sa.String(64), nullable=True))
        batch.add_column(sa.Column("expected_currency", sa.String(8), nullable=True))
        batch.add_column(sa.Column("provider_payment_id", sa.String(128), nullable=True))
        batch.create_unique_constraint("uq_provider_payment", ["payment_provider", "provider_payment_id"])


def downgrade():
    with op.batch_alter_table("transactions") as batch:
        batch.drop_constraint("uq_provider_payment", type_="unique")
        for name in ("provider_payment_id", "expected_currency", "expected_amount", "payment_provider"):
            batch.drop_column(name)
