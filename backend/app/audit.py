"""Small append-only audit writer shared by scans and remediation actions."""
from datetime import datetime, timezone
from uuid import uuid4

from app.remediation_models import AuditEvent


def add_audit(db, event_type, actor, *, finding_id=None, assessment_id=None, before=None, after=None, details=None, occurred_at=None):
    event = AuditEvent(id=f"AUD-{uuid4().hex}", finding_id=finding_id, assessment_id=assessment_id,
                       actor=actor, event_type=event_type,
                       occurred_at=occurred_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                       before=before or {}, after=after or {}, details=details or {})
    db.add(event)
    return event


def finding_snapshot(finding):
    fields = ["status", "owner_employee_id", "due_date", "notes", "revision", "updated_at", "resolved_at", "resolved_result_id", "reopen_count"]
    return {field: getattr(finding, field) for field in fields}
