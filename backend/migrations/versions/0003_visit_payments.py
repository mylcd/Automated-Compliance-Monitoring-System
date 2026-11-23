"""Add visit payments and restaurant/month evidence scope without rewriting history."""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("evidence", sa.Column("period_month", sa.String(7), nullable=True))
    op.add_column("visits", sa.Column("payment_received_minor", sa.Integer(), nullable=True))
    op.add_column("visits", sa.Column("currency", sa.String(3), nullable=True))
    op.execute("ALTER TABLE visits ADD COLUMN sales_transaction_id VARCHAR(100) REFERENCES sales_transactions(id)")
    op.create_index("ix_visits_sales_transaction_id", "visits", ["sales_transaction_id"], unique=True)


def downgrade():
    op.drop_index("ix_visits_sales_transaction_id", table_name="visits")
    op.drop_column("visits", "sales_transaction_id")
    op.drop_column("visits", "currency")
    op.drop_column("visits", "payment_received_minor")
    op.drop_column("evidence", "period_month")
