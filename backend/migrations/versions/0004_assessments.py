"""Persist assessments, immutable results, pinned evidence and stable findings."""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("assessments",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("active_slot", sa.Integer(), nullable=True, unique=True),
        sa.Column("trigger", sa.String(), nullable=False),
        sa.Column("requested_by", sa.String(), nullable=False),
        sa.Column("restaurant_ids", sa.JSON(), nullable=False),
        sa.Column("framework_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("started_at", sa.String()), sa.Column("finished_at", sa.String()),
        sa.Column("evaluated_at", sa.String(), nullable=False),
        sa.Column("total_evaluations", sa.Integer(), nullable=False),
        sa.Column("completed_evaluations", sa.Integer(), nullable=False),
        sa.Column("error", sa.String()),
        sa.Column("policy", sa.JSON(), nullable=False),
        sa.Column("controls_snapshot", sa.JSON(), nullable=False),
        sa.Column("evidence_manifest", sa.JSON(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.CheckConstraint("status IN ('queued','running','completed','failed')", name="ck_assessment_status"),
        sa.CheckConstraint("(status IN ('queued','running') AND active_slot IS NOT NULL AND active_slot = 1) OR (status IN ('completed','failed') AND active_slot IS NULL)", name="ck_assessment_slot"))
    op.create_index("ix_assessments_status", "assessments", ["status"])
    op.create_index("ix_assessments_created_at", "assessments", ["created_at"])
    op.create_table("assessment_results",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("assessment_id", sa.String(), sa.ForeignKey("assessments.id"), nullable=False),
        sa.Column("restaurant_id", sa.String(100), sa.ForeignKey("restaurants.id"), nullable=False),
        sa.Column("control_id", sa.String(), sa.ForeignKey("controls.id"), nullable=False),
        sa.Column("title", sa.String(), nullable=False), sa.Column("rule_version", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False), sa.Column("severity", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False), sa.Column("evaluated_at", sa.String(), nullable=False),
        sa.Column("framework_mappings", sa.JSON(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False), sa.Column("remediation", sa.String(), nullable=False),
        sa.UniqueConstraint("assessment_id", "restaurant_id", "control_id", name="uq_assessment_target_control"),
        sa.CheckConstraint("status IN ('pass','fail','unknown','not_applicable','error')", name="ck_result_status"))
    for field in ["assessment_id", "restaurant_id", "control_id", "status"]:
        op.create_index(f"ix_assessment_results_{field}", "assessment_results", [field])
    op.create_table("result_evidence",
        sa.Column("result_id", sa.String(), sa.ForeignKey("assessment_results.id"), primary_key=True),
        sa.Column("evidence_id", sa.String(), sa.ForeignKey("evidence.id"), primary_key=True),
        sa.Column("snapshot", sa.JSON(), nullable=False))
    op.create_index("ix_result_evidence_evidence_id", "result_evidence", ["evidence_id"])
    op.create_table("findings",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("restaurant_id", sa.String(100), sa.ForeignKey("restaurants.id"), nullable=True),
        sa.Column("target_type", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("affected_restaurant_ids", sa.JSON(), nullable=False),
        sa.Column("control_id", sa.String(), sa.ForeignKey("controls.id"), nullable=False),
        sa.Column("title", sa.String(), nullable=False), sa.Column("severity", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("first_seen_at", sa.String(), nullable=False), sa.Column("last_seen_at", sa.String(), nullable=False),
        sa.Column("occurrences", sa.Integer(), nullable=False),
        sa.Column("first_result_id", sa.String(), sa.ForeignKey("assessment_results.id"), nullable=False),
        sa.Column("last_result_id", sa.String(), sa.ForeignKey("assessment_results.id"), nullable=False),
        sa.Column("reason", sa.String(), nullable=False), sa.Column("remediation", sa.String(), nullable=False),
        sa.UniqueConstraint("target_type", "target_id", "control_id", name="uq_finding_target_control"))
    for field in ["restaurant_id", "control_id", "severity"]:
        op.create_index(f"ix_findings_{field}", "findings", [field])


def downgrade():
    for table in ["findings", "result_evidence", "assessment_results", "assessments"]:
        op.drop_table(table)
