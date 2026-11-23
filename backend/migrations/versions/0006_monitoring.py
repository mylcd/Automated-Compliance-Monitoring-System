"""Persist scan schedules and completion-time finding snapshots."""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("assessments", sa.Column("findings_snapshot", sa.JSON(), nullable=True))
    op.create_table("scan_schedule",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("interval_minutes", sa.Integer(), nullable=False),
        sa.Column("restaurant_ids", sa.JSON(), nullable=False),
        sa.Column("framework_ids", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("updated_by", sa.String(), nullable=False),
        sa.Column("next_run_at", sa.String(), nullable=True),
        sa.Column("last_run_at", sa.String(), nullable=True),
        sa.Column("last_assessment_id", sa.String(), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_schedule_singleton"),
        sa.CheckConstraint("interval_minutes BETWEEN 1 AND 10080", name="ck_schedule_interval"))


def downgrade():
    op.drop_table("scan_schedule")
    op.drop_column("assessments", "findings_snapshot")
