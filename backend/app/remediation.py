"""Controlled mock-source corrections, finding metadata, and audited verification."""
from copy import deepcopy
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import shutil
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, inspect, select, text

from app import domain as d
from app.assessment_models import Assessment, AssessmentResult, Finding, ResultEvidence
from app.audit import add_audit, finding_snapshot
from app.evaluators import outcome
from app.remediation_models import AuditEvent, MockSourceRevision
from app.seed import SOURCE_DATASETS, canonical, current_source_runs, digest, import_sources, load_sources, safe_evidence_path


def utcnow():
    return datetime.now(timezone.utc)


def timestamp():
    return utcnow().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def row_values(row):
    return {column.name: getattr(row, column.name) for column in inspect(type(row)).columns}


def current_sequence(db):
    return db.execute(text("SELECT coalesce(max(rowid), 0) FROM assessments")).scalar_one()


class FindingPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    owner_employee_id: str | None = Field(default=None, min_length=1, max_length=100)
    due_date: str | None = None
    notes: str = Field(default="", max_length=4000)
    status: Literal["open", "in_progress", "awaiting_verification"] | None = None
    expected_revision: int | None = Field(default=None, ge=0)

    @field_validator("due_date")
    @classmethod
    def valid_date(cls, value):
        if value is not None and date.fromisoformat(value).isoformat() != value:
            raise ValueError("Use a YYYY-MM-DD due date")
        return value

    @model_validator(mode="after")
    def changes_required(self):
        if not self.model_fields_set - {"expected_revision"}:
            raise ValueError("Provide at least one remediation field")
        if "status" in self.model_fields_set and self.status is None:
            raise ValueError("Status cannot be null")
        return self


class CorrectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["restore_consent_evidence", "enable_tls"]
    expected_revision: int | None = Field(default=None, ge=0)


def source_bundles(db, settings):
    """Rebuild the current imported source, never a newer/older historical guess."""
    bundles = {}
    for source, run_id in current_source_runs(db).items():
        run = db.get(d.CollectionRun, run_id) if run_id else None
        if run is None or run.status != "completed":
            raise HTTPException(409, f"The current {source} source is unavailable or incomplete. Import complete evidence first.")
        data, base = {}, None
        for evidence in db.scalars(select(d.Evidence).where(d.Evidence.collection_run_id == run_id).order_by(d.Evidence.id)):
            try:
                raw = safe_evidence_path(settings.evidence_dir, evidence.relative_path).read_bytes()
                if digest(raw) != evidence.sha256:
                    raise ValueError("Hash mismatch")
                payload = json.loads(raw)
                if (payload["source_type"] != source or payload["captured_at"] != run.captured_at or
                    payload["source_version"] != run.source_version or payload["dataset_type"] != evidence.dataset_type or
                    not payload["complete"] or not evidence.complete or payload["restaurant_ids"] != evidence.restaurant_ids or
                    len(payload["records"]) != evidence.record_count):
                    raise ValueError("Snapshot metadata mismatch")
                if base is None or evidence.dataset_type != "visits":
                    base = {key: payload[key] for key in ["schema_version", "source_type", "source_version", "captured_at", "complete", "complete_months"]}
                data.setdefault(evidence.dataset_type, []).extend(payload["records"])
            except (OSError, ValueError, KeyError, TypeError):
                raise HTTPException(409, f"The current {source} evidence failed validation. Correct the import before applying a demo correction.") from None
        if base is None or set(data) != set(SOURCE_DATASETS[source]):
            raise HTTPException(409, f"The current {source} source is missing required datasets")
        bundles[source] = {**base, "restaurant_ids": run.restaurant_ids, "datasets": data}
    return bundles


CONSENT_FIX = dict(id="CE-C001-01-FIX", customer_id="C001-01", purpose_id="PUR-MARKETING", event_type="grant", occurred_at="2026-09-01T10:00:00Z", notice_version="privacy-2026-09")


def correction_preview(finding, bundles):
    if finding.target_type != "restaurant":
        return []
    if (finding.target_id, finding.control_id) == ("R001", "LOY-01"):
        events = bundles["loyalty"]["datasets"]["consent_events"]
        prior = next((row for row in events if row["id"] == CONSENT_FIX["id"]), None)
        if prior is not None and prior != CONSENT_FIX:
            raise HTTPException(409, "The historical consent-evidence ID already exists with different values; it cannot be overwritten")
        return [{"action": "restore_consent_evidence", "title": "Restore consent evidence",
                 "description": "Recover the omitted fictional September 1 consent proof for C001-01. This restores historical evidence; it does not grant new consent today.",
                 "already_applied": prior is not None,
                 "changes": [{"dataset": "consent_events", "subject_id": CONSENT_FIX["id"], "field": "consent_event", "before": deepcopy(prior), "after": deepcopy(CONSENT_FIX)}]}]
    if (finding.target_id, finding.control_id) == ("R002", "PAY-01"):
        row = next((row for row in bundles["payment"]["datasets"]["payment_configurations"] if row["asset_id"] == "R002-payment"), None)
        if row is None or not isinstance(row.get("settings_json", {}).get("tls_enabled"), bool):
            raise HTTPException(409, "R002 payment TLS evidence is unavailable")
        return [{"action": "enable_tls", "title": "Enable TLS", "description": "Enable TLS on the fictional R002 payment system. The configured protocol version is retained.",
                 "already_applied": row["settings_json"]["tls_enabled"],
                 "changes": [{"dataset": "payment_configurations", "subject_id": "R002-payment", "field": "settings_json.tls_enabled", "before": row["settings_json"]["tls_enabled"], "after": True}]}]
    return []


def decorate_finding(db, settings, finding, base):
    owner = db.get(d.Employee, finding.owner_employee_id) if finding.owner_employee_id else None
    result = {**base, "owner_name": owner.name if owner else None, "correction_actions": []}
    if finding.target_type == "restaurant" and (finding.target_id, finding.control_id) in [("R001", "LOY-01"), ("R002", "PAY-01")]:
        try:
            result["correction_actions"] = correction_preview(finding, source_bundles(db, settings))
        except HTTPException as error:
            result["correction_error"] = error.detail
    return result


def source_revision_path(settings, relative):
    root = settings.runtime_source_dir
    path = (root / relative).resolve()
    parts = Path(relative).parts
    if len(parts) != 2 or parts[0] != "revisions" or not re.fullmatch(r"SRC-[0-9a-f]{32}", parts[1]) or not path.is_relative_to(root) or path == root:
        raise ValueError("Invalid managed mock-source revision path")
    return path


def write_revision(settings, revision_id, bundles):
    relative = f"revisions/{revision_id}"
    folder = source_revision_path(settings, relative)
    folder.mkdir(parents=True, exist_ok=False)
    files = {}
    for source, original in bundles.items():
        bundle = deepcopy(original)
        if source == "loyalty":
            visits = bundle["datasets"].pop("visits")
            for rid in bundle["restaurant_ids"]:
                rows = [row for row in visits if row["restaurant_id"] == rid]
                months = {row["visited_at"][:7] for row in rows}
                if months != {"2026-10"}:
                    raise ValueError("Runtime demo corrections require the five October visit files")
                files[f"visits/{rid}-2026-10.json"] = canonical(dict(schema_version="1.0", restaurant_id=rid, month="2026-10", currency="USD", captured_at=bundle["captured_at"], records=rows))
        files[f"{source}.json"] = canonical(bundle)
    manifest = {name: digest(contents) for name, contents in sorted(files.items())}
    for name, contents in files.items():
        target = folder / name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(contents)
    manifest_bytes = canonical(manifest)
    (folder / "manifest.json").write_bytes(manifest_bytes)
    # Validate the persisted copy through the same strict reader used by the CLI.
    restored = load_sources(folder)
    if restored != bundles:
        # Source dataset/visit ordering has no business meaning, but keys and each
        # record must survive the round trip exactly.
        for source in bundles:
            for dataset, rows in bundles[source]["datasets"].items():
                if sorted(map(canonical, rows)) != sorted(map(canonical, restored[source]["datasets"][dataset])):
                    raise ValueError("Runtime source copy failed round-trip validation")
    return relative, digest(manifest_bytes)


class RemediationService:
    def __init__(self, settings, factory):
        self.settings, self.factory = settings, factory

    def get(self, db, finding_id):
        finding = db.get(Finding, finding_id)
        if finding is None:
            raise HTTPException(404, "Finding not found")
        return finding

    def check_revision(self, finding, expected):
        if expected is not None and expected != finding.revision:
            raise HTTPException(409, "This finding changed. Refresh it before saving or applying a correction.")

    def patch(self, finding_id, payload, actor):
        with self.factory() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            finding = self.get(db, finding_id)
            self.check_revision(finding, payload.expected_revision)
            changes = payload.model_dump(exclude_unset=True, exclude={"expected_revision"})
            if finding.status == "resolved" and "status" in changes:
                raise HTTPException(409, "A resolved finding can only reopen when a later applicable scan fails")
            if changes.get("owner_employee_id"):
                owner = db.get(d.Employee, changes["owner_employee_id"])
                if owner is None or not owner.active:
                    raise HTTPException(422, "Choose an active mock employee as the remediation owner")
            before = finding_snapshot(finding)
            changed = any(getattr(finding, key) != value for key, value in changes.items())
            if changed:
                for key, value in changes.items():
                    setattr(finding, key, value)
                if "status" in changes and changes["status"] == "awaiting_verification" and before["status"] != "awaiting_verification":
                    finding.verification_after_sequence = current_sequence(db)
                finding.revision += 1
                finding.updated_at = timestamp()
                after = finding_snapshot(finding)
                add_audit(db, "finding_updated", actor, finding_id=finding.id, before=before, after=after, details={"fields": sorted(changes)}, occurred_at=finding.updated_at)
                if before["owner_employee_id"] != after["owner_employee_id"]:
                    add_audit(db, "finding_assigned", actor, finding_id=finding.id, before={"owner_employee_id": before["owner_employee_id"]}, after={"owner_employee_id": after["owner_employee_id"]}, occurred_at=finding.updated_at)
                if before["status"] != after["status"]:
                    add_audit(db, "finding_status_changed", actor, finding_id=finding.id, before={"status": before["status"]}, after={"status": after["status"]}, occurred_at=finding.updated_at)
            db.commit()

    def correct(self, finding_id, payload, actor):
        created_files, folder = [], None
        try:
            with self.factory() as db:
                db.execute(text("BEGIN IMMEDIATE"))
                finding = self.get(db, finding_id)
                self.check_revision(finding, payload.expected_revision)
                if db.scalar(select(Assessment.id).where(Assessment.active_slot == 1)):
                    raise HTTPException(409, "Wait for the queued or running assessment before applying a correction")
                bundles = source_bundles(db, self.settings)
                preview = next((action for action in correction_preview(finding, bundles) if action["action"] == payload.action), None)
                if preview is None:
                    raise HTTPException(422, "This named demo correction is not available for this finding")
                if preview["already_applied"]:
                    # Idempotent retries preserve versions, statuses, and audit history.
                    return {"changed": False, "revision_id": None}
                if finding.status == "resolved":
                    raise HTTPException(409, "A resolved finding does not accept source corrections")
                before = finding_snapshot(finding)
                revision_id = f"SRC-{uuid4().hex}"
                source = "loyalty" if payload.action == "restore_consent_evidence" else "payment"
                if source == "loyalty":
                    bundles[source]["datasets"]["consent_events"].append(deepcopy(CONSENT_FIX))
                else:
                    next(row for row in bundles[source]["datasets"]["payment_configurations"] if row["asset_id"] == "R002-payment")["settings_json"]["tls_enabled"] = True
                at = timestamp()
                bundles[source]["captured_at"] = at
                bundles[source]["source_version"] = f"myl-runtime-{revision_id}"
                if source == "loyalty":
                    # Recollect unchanged ERP approvals with the refreshed loyalty
                    # observation. A newer grant capture needs approval coverage
                    # through that same time; no approval or financial facts change.
                    bundles["erp"]["captured_at"] = at
                    bundles["erp"]["source_version"] = f"myl-runtime-{revision_id}"
                candidate = source_revision_path(self.settings, f"revisions/{revision_id}")
                if candidate.exists():
                    raise HTTPException(409, "A source revision with this ID already exists; no files were changed")
                folder = candidate
                relative, source_hash = write_revision(self.settings, revision_id, bundles)
                imported = import_sources(self.settings, bundles, db=db, created_files=created_files)
                db.add(MockSourceRevision(id=revision_id, finding_id=finding.id, relative_path=relative, sha256=source_hash, created_at=at, created_by=actor, action=payload.action))
                finding.status = "awaiting_verification"
                finding.verification_after_sequence = current_sequence(db)
                finding.revision += 1
                finding.updated_at = at
                add_audit(db, "source_changed", actor, finding_id=finding.id,
                          before={"changes": [{key: change[key] for key in ["dataset", "subject_id", "field", "before"]} for change in preview["changes"]]},
                          after={"changes": [{key: change[key] for key in ["dataset", "subject_id", "field", "after"]} for change in preview["changes"]], "revision_id": revision_id},
                          details={"action": payload.action, "source_hash": source_hash, "relative_path": relative, "imported_sources": imported["imported_sources"]}, occurred_at=at)
                add_audit(db, "finding_status_changed", actor, finding_id=finding.id, before=before, after=finding_snapshot(finding), details={"action": payload.action, "requires_rescan": True}, occurred_at=at)
                db.commit()
                return {"changed": True, "revision_id": revision_id}
        except Exception:
            for path in created_files:
                path.unlink(missing_ok=True)
            if folder is not None and folder.exists():
                shutil.rmtree(folder)
            raise


def target_status(result, finding):
    subjects = [subject for subject in result.details if subject.get("target_type", "restaurant") == finding.target_type and subject.get("target_id", result.restaurant_id) == finding.target_id]
    return outcome(subjects, "Target not applicable.")["status"] if subjects else result.status if result.status in ["unknown", "error"] else "not_applicable"


def verify_findings(db, assessment, results):
    """Only newer, in-scope, current-source passing evidence can resolve a target."""
    sequence = db.execute(text("SELECT rowid FROM assessments WHERE id = :id"), {"id": assessment.id}).scalar_one()
    current_runs = set(current_source_runs(db).values())
    for finding in db.scalars(select(Finding)):
        relevant = [result for result in results if result.control_id == finding.control_id and (result.restaurant_id == finding.restaurant_id if finding.target_type == "restaurant" else result.restaurant_id in finding.affected_restaurant_ids)]
        if not relevant or finding.status == "resolved":
            continue
        states = [target_status(result, finding) for result in relevant]
        if "fail" in states:
            continue
        complete_scope = finding.target_type == "restaurant" or set(finding.affected_restaurant_ids).issubset({result.restaurant_id for result in relevant})
        refs = list(db.scalars(select(ResultEvidence).where(ResultEvidence.result_id.in_([result.id for result in relevant]))))
        sources_current = bool(refs) and all(db.get(d.Evidence, ref.evidence_id).collection_run_id in current_runs for ref in refs)
        valid = complete_scope and all(state == "pass" for state in states) and sources_current and sequence > finding.verification_after_sequence
        if valid:
            before = finding_snapshot(finding)
            finding.status, finding.resolved_at, finding.resolved_result_id = "resolved", assessment.finished_at, relevant[0].id
            finding.updated_at = assessment.finished_at
            finding.revision += 1
            add_audit(db, "finding_resolved", "system", finding_id=finding.id, assessment_id=assessment.id,
                      before=before, after=finding_snapshot(finding), details={"result_ids": [result.id for result in relevant]}, occurred_at=assessment.finished_at)
        elif finding.status == "awaiting_verification":
            add_audit(db, "verification_inconclusive", "system", finding_id=finding.id, assessment_id=assessment.id,
                      before={"status": finding.status}, after={"status": finding.status},
                      details={"result_ids": [result.id for result in relevant], "states": states, "complete_scope": complete_scope, "sources_current": sources_current, "newer_assessment": sequence > finding.verification_after_sequence}, occurred_at=assessment.finished_at)


def create_remediation_router(database, current_session, browser_write, service):
    import secrets
    router = APIRouter(prefix="/api", dependencies=[Depends(current_session)])

    def writer(request: Request, identity=Depends(current_session), _=Depends(browser_write)):
        if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), identity[1].csrf_token):
            raise HTTPException(403, "Invalid CSRF token")
        return identity[0]

    def detail(finding_id, db):
        from app.assessments import finding_detail
        return finding_detail(db, service.settings, service.get(db, finding_id))

    @router.patch("/findings/{finding_id}")
    def patch(finding_id: str, payload: FindingPatch, user=Depends(writer), db=Depends(database)):
        service.patch(finding_id, payload, user.username)
        db.expire_all()
        return detail(finding_id, db)

    @router.post("/findings/{finding_id}/corrections")
    def correct(finding_id: str, payload: CorrectionInput, user=Depends(writer), db=Depends(database)):
        try:
            result = service.correct(finding_id, payload, user.username)
        except (OSError, ValueError) as error:
            raise HTTPException(409, "The demo source correction could not be validated or saved; no change was accepted.") from error
        db.expire_all()
        return {**result, "finding": detail(finding_id, db)}

    @router.get("/audit-events")
    def audit_events(finding_id: str | None = None, assessment_id: str | None = None, event_type: str | None = None, limit: int = Query(25, ge=1, le=200), offset: int = Query(0, ge=0), db=Depends(database)):
        statement = select(AuditEvent)
        for field, value in [("finding_id", finding_id), ("assessment_id", assessment_id), ("event_type", event_type)]:
            if value:
                statement = statement.where(getattr(AuditEvent, field) == value)
        total = db.scalar(select(func.count()).select_from(statement.subquery()))
        items = db.scalars(statement.order_by(AuditEvent.occurred_at.desc(), text("audit_events.rowid DESC")).limit(limit).offset(offset))
        return {"items": [row_values(item) for item in items], "total": total, "limit": limit, "offset": offset}

    return router
