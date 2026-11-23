"""Persisted manual scans, result history, and stable findings for the local demo."""
import logging
import secrets
from collections import Counter
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, inspect, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from app import domain as d
from app.assessment_models import Assessment, AssessmentResult, Finding, ResultEvidence
from app.evaluators import DEPENDENCIES, STATES, SnapshotContext, evaluate_control, outcome
from app.seed import current_source_runs
from app.audit import add_audit, finding_snapshot
from app.remediation import decorate_finding, verify_findings

logger = logging.getLogger(__name__)


def utcnow():
    return datetime.now(timezone.utc)


def stamp(at=None):
    return (at or utcnow()).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def values(row):
    return {c.name: getattr(row, c.name) for c in inspect(type(row)).columns}


class AssessmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    restaurant_ids: list[str] = Field(min_length=1, max_length=5)
    framework_ids: list[Literal["GDPR", "PCI-DSS"]] = Field(min_length=1, max_length=2)

    @field_validator("restaurant_ids", "framework_ids")
    @classmethod
    def unique(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("Selections must not contain duplicates")
        return sorted(value)


def summary_for(assessment, results):
    counts = {status: 0 for status in STATES}
    counts.update(Counter(r.status for r in results))
    applicable = len(results) - counts["not_applicable"]
    complete = assessment.status == "completed"
    covered = Counter(r.restaurant_id for r in results)
    controls_per_restaurant = len(assessment.controls_snapshot)
    return {"counts": counts, "pass_rate": round(counts["pass"] * 100 / applicable, 2) if complete and applicable else None,
            "applicable_evaluations": applicable, "total_evaluations": assessment.total_evaluations,
            "coverage": {"total_restaurants": assessment.policy["total_restaurants"], "in_scope_restaurants": len(assessment.restaurant_ids),
                         "assessed_restaurants": sum(count == controls_per_restaurant for count in covered.values()) if complete else 0}}


def assessment_dict(item):
    return {k: v for k, v in values(item).items() if k not in ["active_slot", "controls_snapshot", "evidence_manifest", "findings_snapshot"]}


def result_dict(db, item):
    return {**values(item), "evidence": [ref.snapshot for ref in db.scalars(select(ResultEvidence).where(ResultEvidence.result_id == item.id).order_by(ResultEvidence.evidence_id))]}


def latest_result(db, restaurant, control=None):
    query = select(AssessmentResult).join(Assessment).where(Assessment.status == "completed", AssessmentResult.restaurant_id == restaurant)
    if control:
        query = query.where(AssessmentResult.control_id == control)
    return db.scalar(query.order_by(Assessment.created_at.desc(), text("assessments.rowid DESC"), AssessmentResult.control_id).limit(1))


def finding_dict(db, item):
    if item.target_type == "shared_system":
        latest = db.scalar(select(AssessmentResult).join(Assessment).where(Assessment.status == "completed", AssessmentResult.control_id == item.control_id, AssessmentResult.restaurant_id.in_(item.affected_restaurant_ids)).order_by(Assessment.created_at.desc(), text("assessments.rowid DESC")).limit(1))
    else:
        latest = latest_result(db, item.restaurant_id, item.control_id)
    if latest:
        subjects = [s for s in latest.details if s.get("target_type", "restaurant") == item.target_type and s.get("target_id", latest.restaurant_id) == item.target_id]
        latest_status = outcome(subjects, "Shared system is not applicable.")["status"] if subjects else latest.status if latest.status in ["unknown", "error"] else "not_applicable"
    else:
        latest_status = None
    owner = db.get(d.Employee, item.owner_employee_id) if item.owner_employee_id else None
    return {**values(item), "latest_evaluation_status": latest_status, "owner_name": owner.name if owner else None}


def finding_detail(db, settings, item):
    query = select(AssessmentResult).join(Assessment).where(Assessment.status == "completed", AssessmentResult.control_id == item.control_id, AssessmentResult.status == "fail")
    if item.target_type == "restaurant":
        query = query.where(AssessmentResult.restaurant_id == item.restaurant_id)
    history, seen = [], set()
    for row in db.scalars(query.order_by(Assessment.created_at.desc(), text("assessments.rowid DESC"), AssessmentResult.restaurant_id)):
        if row.assessment_id in seen or not any(s.get("target_type", "restaurant") == item.target_type and s.get("target_id", row.restaurant_id) == item.target_id and s["status"] == "fail" for s in row.details):
            continue
        seen.add(row.assessment_id)
        history.append(values(row))
    return decorate_finding(db, settings, item, {**finding_dict(db, item), "result": result_dict(db, db.get(AssessmentResult, item.last_result_id)), "history": history})


class AssessmentService:
    def __init__(self, settings, factory):
        self.settings, self.factory = settings, factory

    def recover_interrupted(self):
        with self.factory.begin() as db:
            for item in db.scalars(select(Assessment).where(Assessment.active_slot == 1)):
                before = {"status": item.status, "completed_evaluations": item.completed_evaluations}
                item.status, item.active_slot, item.finished_at = "failed", None, stamp()
                item.error = "Assessment interrupted by a backend restart. Retry to create a new assessment."
                add_audit(db, "assessment_interrupted", "system", assessment_id=item.id, before=before, after={"status": "failed"}, occurred_at=item.finished_at)

    def queue(self, payload, username, *, trigger="manual", before_snapshot=None, on_queued=None):
        with self.factory() as db:
            # SQLite serializes this short reservation; the unique slot also protects
            # against requests handled by separate threads/processes.
            db.execute(text("BEGIN IMMEDIATE"))
            if db.scalar(select(Assessment.id).where(Assessment.active_slot == 1)):
                raise HTTPException(409, "An assessment is already queued or running. Wait for it to finish.")
            if before_snapshot:
                before_snapshot(db)
            restaurants = list(db.scalars(select(d.Restaurant.id).where(d.Restaurant.id.in_(payload.restaurant_ids))))
            if set(restaurants) != set(payload.restaurant_ids):
                raise HTTPException(404, "One or more selected restaurants do not exist")
            controls = list(db.scalars(select(d.Control).where(d.Control.id.in_(select(d.ControlMapping.control_id).where(d.ControlMapping.framework_id.in_(payload.framework_ids)))).order_by(d.Control.id)))
            if not controls:
                raise HTTPException(409, "No controls are configured for the selected frameworks")
            snapshots = []
            for control in controls:
                mappings = [values(m) for m in db.scalars(select(d.ControlMapping).where(d.ControlMapping.control_id == control.id).order_by(d.ControlMapping.framework_id, d.ControlMapping.requirement_ref))]
                snapshots.append({"id": control.id, "title": control.title, "rule_version": control.rule_version, "severity": control.severity, "framework_mappings": mappings})
            required = {"restaurants", "assets"}
            for control in controls:
                required.update(DEPENDENCIES[control.id])
            # A prior source bundle can be selected again. Its original import time
            # is historical, so it must not lose to a newer but no-longer-current run.
            active_runs = {run for run in current_source_runs(db).values() if run}
            candidates = list(db.execute(select(d.Evidence, d.CollectionRun.status).join(d.CollectionRun).where(d.Evidence.dataset_type.in_(required), d.CollectionRun.id.in_(active_runs)).order_by(d.CollectionRun.imported_at.desc(), d.CollectionRun.id.desc(), d.Evidence.id.desc())))
            manifest = {}
            for restaurant in payload.restaurant_ids:
                manifest[restaurant] = {}
                for dataset in sorted(required):
                    selected = next(((item, status) for item, status in candidates if item.dataset_type == dataset and restaurant in item.restaurant_ids), None)
                    manifest[restaurant][dataset] = {**values(selected[0]), "collection_status": selected[1]} if selected else None
            created = stamp()
            item = Assessment(id=f"A-{uuid4().hex}", status="queued", active_slot=1, trigger=trigger, requested_by=username,
                              restaurant_ids=payload.restaurant_ids, framework_ids=payload.framework_ids,
                              created_at=created, started_at=None, finished_at=None, evaluated_at=created,
                              total_evaluations=len(controls) * len(restaurants), completed_evaluations=0, error=None,
                              policy={"max_evidence_age_days": self.settings.assessment_max_evidence_age_days, "total_restaurants": db.scalar(select(func.count()).select_from(d.Restaurant)), "evidence_basis": "Current imported snapshots pinned when queued; source collection is performed by the local seed connector."},
                              controls_snapshot=snapshots, evidence_manifest=manifest, summary={})
            item.summary = summary_for(item, [])
            db.add(item)
            db.flush()
            if on_queued:
                on_queued(db, item)
            add_audit(db, "assessment_queued", username, assessment_id=item.id, after={"status": "queued"}, details={"restaurant_ids": item.restaurant_ids, "framework_ids": item.framework_ids}, occurred_at=created)
            try:
                db.commit()
            except IntegrityError:
                raise HTTPException(409, "An assessment is already queued or running.") from None
            return assessment_dict(item)

    def run(self, assessment_id):
        try:
            with self.factory.begin() as db:
                # An atomic claim prevents executing a queued assessment twice.
                claimed = db.execute(update(Assessment).where(Assessment.id == assessment_id, Assessment.status == "queued").values(status="running", started_at=stamp())).rowcount
                if not claimed:
                    return
                item = db.get(Assessment, assessment_id)
                snapshot = values(item)
                add_audit(db, "assessment_started", "system", assessment_id=item.id, before={"status": "queued"}, after={"status": "running"}, occurred_at=item.started_at)
            context = SnapshotContext(self.settings, snapshot["evidence_manifest"], snapshot["evaluated_at"], snapshot["policy"]["max_evidence_age_days"])
            for restaurant in snapshot["restaurant_ids"]:
                for control in snapshot["controls_snapshot"]:
                    evaluation = evaluate_control(control["id"], restaurant, context)
                    with self.factory.begin() as db:
                        # Acquire the write lock and fence a worker whose scan was
                        # marked interrupted before it can append another result.
                        progressed = db.execute(update(Assessment).where(Assessment.id == assessment_id, Assessment.status == "running", Assessment.active_slot == 1).values(completed_evaluations=Assessment.completed_evaluations + 1)).rowcount
                        if not progressed:
                            return
                        result = AssessmentResult(id=f"RES-{uuid4().hex}", assessment_id=assessment_id, restaurant_id=restaurant,
                                                  control_id=control["id"], title=control["title"], rule_version=control["rule_version"],
                                                  status=evaluation["status"], severity=control["severity"], reason=evaluation["reason"],
                                                  evaluated_at=snapshot["evaluated_at"], framework_mappings=control["framework_mappings"],
                                                  details=evaluation["details"], remediation=evaluation["remediation"])
                        db.add(result)
                        db.flush()
                        for evidence in evaluation["evidence"]:
                            db.add(ResultEvidence(result_id=result.id, evidence_id=evidence["id"], snapshot=evidence))
                        item = db.get(Assessment, assessment_id)
                        item.summary = summary_for(item, list(db.scalars(select(AssessmentResult).where(AssessmentResult.assessment_id == assessment_id))))
            with self.factory.begin() as db:
                completed = db.execute(update(Assessment).where(Assessment.id == assessment_id, Assessment.status == "running", Assessment.active_slot == 1).values(status="completed", active_slot=None, finished_at=stamp())).rowcount
                if not completed:
                    return
                item = db.get(Assessment, assessment_id)
                results = list(db.scalars(select(AssessmentResult).where(AssessmentResult.assessment_id == assessment_id)))
                item.summary = summary_for(item, results)
                sequence = db.execute(text("SELECT rowid FROM assessments WHERE id = :id"), {"id": item.id}).scalar_one()
                current_runs = set(current_source_runs(db).values())
                groups = {}
                for result in sorted(results, key=lambda r: (r.restaurant_id, r.control_id)):
                    if result.status != "fail":
                        continue
                    for detail in result.details:
                        if detail["status"] != "fail":
                            continue
                        asset = detail.get("asset_id")
                        key = (detail.get("target_type", "restaurant"), detail.get("target_id", result.restaurant_id), result.control_id)
                        group = groups.setdefault(key, {"result": result, "restaurants": set(), "details": []})
                        group["restaurants"].add(result.restaurant_id)
                        if not any((s["subject_type"], s["subject_id"], s.get("asset_id")) == (detail["subject_type"], detail["subject_id"], asset) for s in group["details"]):
                            group["details"].append(detail)
                for (target_type, target_id, control_id), group in groups.items():
                    result = group["result"]
                    reason = outcome(group["details"], "No failed subjects.")["reason"]
                    finding = db.scalar(select(Finding).where(Finding.target_type == target_type, Finding.target_id == target_id, Finding.control_id == control_id))
                    if finding:
                        before = finding_snapshot(finding)
                        finding.last_result_id, finding.last_seen_at = result.id, item.evaluated_at
                        finding.occurrences += 1
                        finding.reason, finding.remediation = reason, result.remediation
                        finding.affected_restaurant_ids = sorted(set(finding.affected_restaurant_ids) | group["restaurants"])
                        finding.severity = result.severity
                        if finding.status == "resolved":
                            finding.status = "open"
                            finding.resolved_at = finding.resolved_result_id = None
                            finding.reopen_count += 1
                        elif finding.status == "awaiting_verification":
                            refs = list(db.scalars(select(ResultEvidence).where(ResultEvidence.result_id == result.id)))
                            current_evidence = bool(refs) and all(db.get(d.Evidence, ref.evidence_id).collection_run_id in current_runs for ref in refs)
                            if sequence > finding.verification_after_sequence and current_evidence:
                                finding.status = "in_progress"
                        finding.revision += 1
                        finding.updated_at = item.finished_at
                        event_type = "finding_reopened" if before["status"] == "resolved" else "verification_failed" if before["status"] == "awaiting_verification" and finding.status == "in_progress" else "finding_observed"
                        add_audit(db, event_type, "system", finding_id=finding.id, assessment_id=item.id, before=before, after=finding_snapshot(finding), details={"result_id": result.id}, occurred_at=item.finished_at)
                    else:
                        finding = Finding(id=f"F-{uuid4().hex}", restaurant_id=result.restaurant_id if target_type == "restaurant" else None,
                                       target_type=target_type, target_id=target_id, affected_restaurant_ids=sorted(group["restaurants"]), control_id=result.control_id,
                                       title=result.title, severity=result.severity, status="open", first_seen_at=item.evaluated_at,
                                       last_seen_at=item.evaluated_at, occurrences=1, first_result_id=result.id,
                                       last_result_id=result.id, reason=reason, remediation=result.remediation, updated_at=item.finished_at)
                        db.add(finding)
                        db.flush()
                        add_audit(db, "finding_created", "system", finding_id=finding.id, assessment_id=item.id, after=finding_snapshot(finding), details={"result_id": result.id}, occurred_at=item.finished_at)
                verify_findings(db, item, results)
                # Capture workflow after verification, in the completion transaction.
                # Later assignments and scans cannot rewrite an older report.
                from app.reporting import capture_findings
                db.flush()
                item.findings_snapshot = capture_findings(db, item, results)
                add_audit(db, "assessment_completed", "system", assessment_id=item.id, before={"status": "running"}, after={"status": "completed", "summary": item.summary}, occurred_at=item.finished_at)
        except Exception:
            logger.exception("Assessment %s failed", assessment_id)
            with self.factory.begin() as db:
                item = db.get(Assessment, assessment_id)
                if item and item.status in ["queued", "running"]:
                    item.status, item.active_slot, item.finished_at = "failed", None, stamp()
                    item.error = "Assessment execution failed. Existing results are retained; retry creates a new assessment."
                    add_audit(db, "assessment_failed", "system", assessment_id=item.id, before={"status": "running"}, after={"status": "failed"}, occurred_at=item.finished_at)


def create_assessment_router(database, current_session, browser_write, service):
    router = APIRouter(prefix="/api", dependencies=[Depends(current_session)])

    def write_access(request: Request, identity=Depends(current_session), _=Depends(browser_write)):
        if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), identity[1].csrf_token):
            raise HTTPException(403, "Invalid CSRF token")
        return identity[0]

    def assessment_or_404(db, assessment_id):
        item = db.get(Assessment, assessment_id)
        if item is None:
            raise HTTPException(404, "Assessment not found")
        return item

    def paginate(db, statement, limit, offset, serialize):
        count = db.scalar(select(func.count()).select_from(statement.order_by(None).subquery()))
        return {"items": [serialize(row) for row in db.scalars(statement.limit(limit).offset(offset))], "total": count, "limit": limit, "offset": offset}

    @router.post("/assessments", status_code=202)
    def start(payload: AssessmentInput, request: Request, tasks: BackgroundTasks, user=Depends(write_access)):
        result = service.queue(payload, user.username)
        tasks.add_task(request.app.state.assessment_runner, result["id"])
        return result

    @router.post("/assessments/{assessment_id}/retry", status_code=202)
    def retry(assessment_id: str, request: Request, tasks: BackgroundTasks, user=Depends(write_access), db=Depends(database)):
        previous = assessment_or_404(db, assessment_id)
        if previous.status != "failed":
            raise HTTPException(409, "Only failed assessments can be retried")
        result = service.queue(AssessmentInput(restaurant_ids=previous.restaurant_ids, framework_ids=previous.framework_ids), user.username)
        tasks.add_task(request.app.state.assessment_runner, result["id"])
        return result

    @router.get("/assessments")
    def assessments(limit: int = Query(25, ge=1, le=200), offset: int = Query(0, ge=0), db=Depends(database)):
        return paginate(db, select(Assessment).order_by(Assessment.created_at.desc(), text("assessments.rowid DESC")), limit, offset, assessment_dict)

    @router.get("/assessments/{assessment_id}")
    def detail(assessment_id: str, db=Depends(database)):
        item = assessment_or_404(db, assessment_id)
        results = db.scalars(select(AssessmentResult).where(AssessmentResult.assessment_id == assessment_id).order_by(AssessmentResult.restaurant_id, AssessmentResult.control_id))
        return {**assessment_dict(item), "controls": item.controls_snapshot, "results": [result_dict(db, row) for row in results]}

    @router.get("/assessments/{assessment_id}/results")
    def results(assessment_id: str, restaurant_id: str | None = None, status: Literal["pass", "fail", "unknown", "not_applicable", "error"] | None = None, control_id: str | None = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), db=Depends(database)):
        assessment_or_404(db, assessment_id)
        query = select(AssessmentResult).where(AssessmentResult.assessment_id == assessment_id)
        for field, value in [("restaurant_id", restaurant_id), ("status", status), ("control_id", control_id)]:
            if value:
                query = query.where(getattr(AssessmentResult, field) == value)
        return paginate(db, query.order_by(AssessmentResult.restaurant_id, AssessmentResult.control_id), limit, offset, lambda row: result_dict(db, row))

    @router.get("/results/{result_id}")
    def result(result_id: str, db=Depends(database)):
        item = db.get(AssessmentResult, result_id)
        if item is None:
            raise HTTPException(404, "Result not found")
        return result_dict(db, item)

    @router.get("/findings")
    def findings(restaurant_id: str | None = None, severity: Literal["critical", "high", "medium", "low"] | None = None, control_id: str | None = None, status: Literal["all", "open", "in_progress", "awaiting_verification", "resolved"] | None = None, limit: int = Query(25, ge=1, le=200), offset: int = Query(0, ge=0), db=Depends(database)):
        query = select(Finding)
        if restaurant_id:
            query = query.where(or_(Finding.restaurant_id == restaurant_id, Finding.affected_restaurant_ids.contains(restaurant_id)))
        for field, value in [("severity", severity), ("control_id", control_id), ("status", status if status != "all" else None)]:
            if value:
                query = query.where(getattr(Finding, field) == value)
        return paginate(db, query.order_by(Finding.last_seen_at.desc(), Finding.restaurant_id, Finding.control_id), limit, offset, lambda row: finding_dict(db, row))

    @router.get("/findings/{finding_id}")
    def finding(finding_id: str, db=Depends(database)):
        item = db.get(Finding, finding_id)
        if item is None:
            raise HTTPException(404, "Finding not found")
        return finding_detail(db, service.settings, item)

    return router
