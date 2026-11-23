"""Assessment history and stable findings; imported evidence remains immutable."""
from sqlalchemy import JSON, CheckConstraint, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class Assessment(Base):
    __tablename__ = "assessments"
    __table_args__ = (
        CheckConstraint("status IN ('queued','running','completed','failed')", name="ck_assessment_status"),
        CheckConstraint("(status IN ('queued','running') AND active_slot IS NOT NULL AND active_slot = 1) OR (status IN ('completed','failed') AND active_slot IS NULL)", name="ck_assessment_slot"),
    )
    id: Mapped[str] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(index=True)
    active_slot: Mapped[int | None] = mapped_column(unique=True)
    trigger: Mapped[str]
    requested_by: Mapped[str]
    restaurant_ids: Mapped[list] = mapped_column(JSON)
    framework_ids: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(index=True)
    started_at: Mapped[str | None]
    finished_at: Mapped[str | None]
    evaluated_at: Mapped[str]
    total_evaluations: Mapped[int]
    completed_evaluations: Mapped[int]
    error: Mapped[str | None]
    policy: Mapped[dict] = mapped_column(JSON)
    controls_snapshot: Mapped[list] = mapped_column(JSON)
    evidence_manifest: Mapped[dict] = mapped_column(JSON)
    summary: Mapped[dict] = mapped_column(JSON)
    # None identifies pre-Phase-5 history, whose workflow state was not captured.
    findings_snapshot: Mapped[list | None] = mapped_column(JSON, nullable=True)


class AssessmentResult(Base):
    __tablename__ = "assessment_results"
    __table_args__ = (
        UniqueConstraint("assessment_id", "restaurant_id", "control_id", name="uq_assessment_target_control"),
        CheckConstraint("status IN ('pass','fail','unknown','not_applicable','error')", name="ck_result_status"),
    )
    id: Mapped[str] = mapped_column(primary_key=True)
    assessment_id: Mapped[str] = mapped_column(ForeignKey("assessments.id"), index=True)
    restaurant_id: Mapped[str] = mapped_column(ForeignKey("restaurants.id"), index=True)
    control_id: Mapped[str] = mapped_column(ForeignKey("controls.id"), index=True)
    title: Mapped[str]
    rule_version: Mapped[str]
    status: Mapped[str] = mapped_column(index=True)
    severity: Mapped[str]
    reason: Mapped[str]
    evaluated_at: Mapped[str]
    framework_mappings: Mapped[list] = mapped_column(JSON)
    details: Mapped[list] = mapped_column(JSON)
    remediation: Mapped[str]


class ResultEvidence(Base):
    __tablename__ = "result_evidence"
    result_id: Mapped[str] = mapped_column(ForeignKey("assessment_results.id"), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(ForeignKey("evidence.id"), primary_key=True, index=True)
    snapshot: Mapped[dict] = mapped_column(JSON)


class Finding(Base):
    __tablename__ = "findings"
    __table_args__ = (UniqueConstraint("target_type", "target_id", "control_id", name="uq_finding_target_control"),)
    id: Mapped[str] = mapped_column(primary_key=True)
    restaurant_id: Mapped[str | None] = mapped_column(ForeignKey("restaurants.id"), index=True)
    target_type: Mapped[str]
    target_id: Mapped[str]
    affected_restaurant_ids: Mapped[list] = mapped_column(JSON)
    control_id: Mapped[str] = mapped_column(ForeignKey("controls.id"), index=True)
    title: Mapped[str]
    severity: Mapped[str] = mapped_column(index=True)
    status: Mapped[str]
    first_seen_at: Mapped[str]
    last_seen_at: Mapped[str]
    occurrences: Mapped[int]
    first_result_id: Mapped[str] = mapped_column(ForeignKey("assessment_results.id"))
    last_result_id: Mapped[str] = mapped_column(ForeignKey("assessment_results.id"))
    reason: Mapped[str]
    remediation: Mapped[str]
    owner_employee_id: Mapped[str | None] = mapped_column(ForeignKey("employees.id"), index=True)
    due_date: Mapped[str | None] = mapped_column()
    notes: Mapped[str] = mapped_column(default="", server_default="")
    revision: Mapped[int] = mapped_column(default=0, server_default="0")
    updated_at: Mapped[str | None]
    resolved_at: Mapped[str | None]
    resolved_result_id: Mapped[str | None] = mapped_column(ForeignKey("assessment_results.id"))
    reopen_count: Mapped[int] = mapped_column(default=0, server_default="0")
    verification_after_sequence: Mapped[int] = mapped_column(default=0, server_default="0")
