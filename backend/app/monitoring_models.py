"""One persistent schedule; assessment.active_slot serializes every trigger."""
from sqlalchemy import JSON, CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class ScanSchedule(Base):
    __tablename__ = "scan_schedule"
    __table_args__ = (CheckConstraint("id = 1", name="ck_schedule_singleton"),
                      CheckConstraint("interval_minutes BETWEEN 1 AND 10080", name="ck_schedule_interval"))
    id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=False)
    interval_minutes: Mapped[int] = mapped_column(default=60)
    restaurant_ids: Mapped[list] = mapped_column(JSON)
    framework_ids: Mapped[list] = mapped_column(JSON)
    revision: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[str]
    updated_by: Mapped[str]
    next_run_at: Mapped[str | None]
    last_run_at: Mapped[str | None]
    last_assessment_id: Mapped[str | None]
    last_error: Mapped[str | None]
