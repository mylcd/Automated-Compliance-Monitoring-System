"""Add remediation ownership, verified lifecycle, audit, and runtime source lineage."""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    # Native nullable REFERENCES preserves existing rows and their historical IDs.
    op.execute("ALTER TABLE findings ADD COLUMN owner_employee_id VARCHAR(100) REFERENCES employees(id)")
    op.add_column("findings", sa.Column("due_date", sa.String(), nullable=True))
    op.add_column("findings", sa.Column("notes", sa.String(), nullable=False, server_default=""))
    op.add_column("findings", sa.Column("revision", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("findings", sa.Column("updated_at", sa.String(), nullable=True))
    op.add_column("findings", sa.Column("resolved_at", sa.String(), nullable=True))
    op.execute("ALTER TABLE findings ADD COLUMN resolved_result_id VARCHAR REFERENCES assessment_results(id)")
    op.add_column("findings", sa.Column("reopen_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("findings", sa.Column("verification_after_sequence", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_findings_owner_employee_id", "findings", ["owner_employee_id"])
    op.create_table("audit_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("finding_id", sa.String(), sa.ForeignKey("findings.id")),
        sa.Column("assessment_id", sa.String(), sa.ForeignKey("assessments.id")),
        sa.Column("actor", sa.String(), nullable=False), sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.String(), nullable=False),
        sa.Column("before", sa.JSON(), nullable=False), sa.Column("after", sa.JSON(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False))
    for column in ["finding_id", "assessment_id", "event_type", "occurred_at"]:
        op.create_index(f"ix_audit_events_{column}", "audit_events", [column])
    op.create_table("mock_source_revisions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("finding_id", sa.String(), sa.ForeignKey("findings.id"), nullable=False),
        sa.Column("relative_path", sa.String(), nullable=False, unique=True),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False), sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False))
    op.create_index("ix_mock_source_revisions_finding_id", "mock_source_revisions", ["finding_id"])


def downgrade():
    op.drop_table("mock_source_revisions")
    op.drop_table("audit_events")
    op.drop_index("ix_findings_owner_employee_id", table_name="findings")
    for column in ["verification_after_sequence", "reopen_count", "resolved_result_id", "resolved_at", "updated_at", "revision", "notes", "due_date", "owner_employee_id"]:
        op.drop_column("findings", column)
