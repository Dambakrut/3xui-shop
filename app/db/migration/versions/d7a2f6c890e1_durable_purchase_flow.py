"""Reserve purchase flows before provider invoice creation.

Revision ID: d7a2f6c890e1
Revises: c4e91b2a70d5
"""
import sqlalchemy as sa
from alembic import op

revision = "d7a2f6c890e1"
down_revision = "c4e91b2a70d5"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("transactions") as batch:
        batch.add_column(sa.Column("purchase_flow_id", sa.String(32), nullable=True))
        batch.add_column(sa.Column("payment_url", sa.Text(), nullable=True))
        batch.create_unique_constraint("uq_transaction_purchase_flow", ["purchase_flow_id"])


def downgrade():
    # Old code cannot honor a durable reservation. Refuse to silently remove
    # protection while any checkout/payment still needs processing or review.
    active = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM transactions WHERE purchase_flow_id IS NOT NULL "
        "AND status IN ('pending', 'processing', 'review_required')"
    )).scalar_one()
    if active:
        raise RuntimeError("Resolve active purchase flows before downgrade")
    with op.batch_alter_table("transactions") as batch:
        batch.drop_constraint("uq_transaction_purchase_flow", type_="unique")
        batch.drop_column("payment_url")
        batch.drop_column("purchase_flow_id")
