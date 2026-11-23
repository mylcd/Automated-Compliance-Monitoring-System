"""Append-only audit entries and accepted immutable runtime mock-source versions."""
from sqlalchemy import JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(primary_key=True)
    finding_id: Mapped[str | None] = mapped_column(ForeignKey("findings.id"), index=True)
    assessment_id: Mapped[str | None] = mapped_column(ForeignKey("assessments.id"), index=True)
    actor: Mapped[str]
    event_type: Mapped[str] = mapped_column(index=True)
    occurred_at: Mapped[str] = mapped_column(index=True)
    before: Mapped[dict] = mapped_column(JSON)
    after: Mapped[dict] = mapped_column(JSON)
    details: Mapped[dict] = mapped_column(JSON)


class MockSourceRevision(Base):
    __tablename__ = "mock_source_revisions"
    id: Mapped[str] = mapped_column(primary_key=True)
    finding_id: Mapped[str] = mapped_column(ForeignKey("findings.id"), index=True)
    relative_path: Mapped[str] = mapped_column(unique=True)
    sha256: Mapped[str]
    created_at: Mapped[str]
    created_by: Mapped[str]
    action: Mapped[str]
