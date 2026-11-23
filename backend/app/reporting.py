"""One filtered read model for dashboards, historical finding lists and YAML."""
from collections import Counter
from datetime import datetime
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, text
import yaml

from app.assessment_models import Assessment, AssessmentResult, Finding, ResultEvidence
from app.assessments import assessment_dict, stamp
from app.evaluators import STATES
from app.remediation import target_status

SEVERITIES = ("critical", "high", "medium", "low")


def aggregate_status(statuses):
    return next((s for s in ("fail", "error", "unknown", "pass") if s in statuses), "not_applicable")


def capture_findings(db, assessment, results):
    """Called inside assessment completion, after verified lifecycle changes."""
    snapshots = []
    for finding in db.scalars(select(Finding).order_by(Finding.id)):
        matches = [r for r in results if r.control_id == finding.control_id and
                   r.restaurant_id in finding.affected_restaurant_ids]
        if not matches:
            continue
        snapshots.append({key: getattr(finding, key) for key in (
            "id", "target_type", "target_id", "restaurant_id", "affected_restaurant_ids",
            "control_id", "title", "severity", "status", "owner_employee_id", "due_date",
            "first_seen_at", "last_seen_at", "resolved_at", "resolved_result_id", "reason", "remediation")})
        states = {result.id: target_status(result, finding) for result in matches}
        snapshots[-1].update(evaluation_status=aggregate_status(states.values()), result_statuses=states,
            result_ids=[r.id for r in matches], snapshot_at=assessment.finished_at)
    return snapshots


def legacy_observations(results):
    """Historical failures are factual; historical owner/due/status are unknown."""
    grouped = {}
    for result in results:
        if result.status != "fail":
            continue
        for detail in result.details:
            if detail["status"] != "fail":
                continue
            target = (detail.get("target_type", "restaurant"), detail.get("target_id", result.restaurant_id), result.control_id)
            item = grouped.setdefault(target, dict(id=f"OBS-{target[0]}-{target[1]}-{target[2]}",
                target_type=target[0], target_id=target[1], restaurant_id=result.restaurant_id if target[0] == "restaurant" else None,
                affected_restaurant_ids=[], control_id=result.control_id, title=result.title, severity=result.severity,
                status="unrecorded", owner_employee_id=None, due_date=None, evaluation_status="fail", result_ids=[],
                reason=result.reason, remediation=result.remediation, snapshot_at=None))
            if result.restaurant_id not in item["affected_restaurant_ids"]:
                item["affected_restaurant_ids"].append(result.restaurant_id)
            if result.id not in item["result_ids"]:
                item["result_ids"].append(result.id)
    return list(grouped.values())


class Coverage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total_restaurants: int
    in_scope_restaurants: int
    assessed_restaurants: int
    unassessed_restaurants: int
    out_of_scope_restaurants: int


class ResultCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pass_: int = Field(alias="pass", ge=0)
    fail: int = Field(ge=0)
    unknown: int = Field(ge=0)
    not_applicable: int = Field(ge=0)
    error: int = Field(ge=0)


class Rollup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    counts: ResultCounts
    total_evaluations: int
    applicable_evaluations: int
    pass_rate: float | None


class FrameworkRollup(Rollup):
    id: Literal["GDPR", "PCI-DSS"]


class ReportSummary(Rollup):
    findings: int
    active_findings: int | None
    resolved_findings: int | None
    overdue_findings: int | None
    severity_counts: dict[str, int]
    workflow_snapshot_available: bool


class ReportResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    restaurant_id: str
    control_id: str
    title: str
    rule_version: str
    status: Literal["pass", "fail", "unknown", "not_applicable", "error"]
    severity: Literal["critical", "high", "medium", "low"]
    reason: str
    evaluated_at: str
    remediation: str
    framework_mappings: list[dict]
    evidence_ids: list[str]


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    dataset_type: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    captured_at: str
    restaurant_ids: list[str]
    age_days_at_assessment: float | None
    stale: bool | None


class ReportFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    target_type: Literal["restaurant", "shared_system"]
    target_id: str
    restaurant_id: str | None
    affected_restaurant_ids: list[str]
    in_scope_restaurant_ids: list[str]
    control_id: str
    title: str
    severity: Literal["critical", "high", "medium", "low"]
    status: Literal["open", "in_progress", "awaiting_verification", "resolved", "unrecorded"]
    owner_employee_id: str | None
    due_date: str | None
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    resolved_at: str | None = None
    resolved_result_id: str | None = None
    evaluation_status: Literal["pass", "fail", "unknown", "not_applicable", "error"]
    result_ids: list[str]
    result_statuses: dict[str, str] = Field(default_factory=dict)
    snapshot_at: str | None
    overdue: bool | None
    reason: str
    remediation: str


class ReportDocument(BaseModel):
    """Version 1 exports assessment facts, never source payloads or auth rows."""
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"]
    report_id: str
    generated_at: str
    organization: str
    assessment: dict
    filters: dict
    coverage: Coverage
    summary: ReportSummary
    frameworks: list[FrameworkRollup]
    results: list[ReportResult]
    findings: list[ReportFinding]
    evidence_references: list[EvidenceReference]
    evidence_health: dict
    limitations: list[str]


def rollup(results):
    counts = {state: 0 for state in STATES}
    counts.update(Counter(row.status for row in results))
    applicable = len(results) - counts["not_applicable"]
    return dict(counts=counts, total_evaluations=len(results), applicable_evaluations=applicable,
                pass_rate=round(100 * counts["pass"] / applicable, 2) if applicable else None)


def report_data(db, assessment_id, restaurant_id=None, framework_id=None, severity=None):
    item = db.get(Assessment, assessment_id)
    if item is None:
        raise HTTPException(404, "Assessment not found")
    if item.status != "completed":
        raise HTTPException(409, f"Assessment is {item.status}. Select a completed assessment for a dashboard or report.")
    if (not {"total_restaurants", "max_evidence_age_days"} <= item.policy.keys() or
        any(not {"id", "severity", "rule_version", "framework_mappings"} <= control.keys() for control in item.controls_snapshot)):
        raise HTTPException(409, "This historical assessment lacks versioned report metadata. Select another completed scan or run a new assessment.")
    if restaurant_id and restaurant_id not in item.restaurant_ids:
        raise HTTPException(422, "Restaurant is outside this assessment's scope")
    if framework_id and framework_id not in item.framework_ids:
        raise HTTPException(422, "Framework is outside this assessment's scope")
    restaurants = [restaurant_id] if restaurant_id else item.restaurant_ids
    frameworks = [framework_id] if framework_id else item.framework_ids
    controls = [c for c in item.controls_snapshot if
                (not severity or c["severity"] == severity) and any(m["framework_id"] in frameworks for m in c["framework_mappings"])]
    control_ids = {c["id"] for c in controls}
    results = [r for r in db.scalars(select(AssessmentResult).where(AssessmentResult.assessment_id == item.id)
               .order_by(AssessmentResult.restaurant_id, AssessmentResult.control_id))
               if r.restaurant_id in restaurants and r.control_id in control_ids]
    known_workflow = item.findings_snapshot is not None
    findings = [dict(f) for f in (item.findings_snapshot if known_workflow else legacy_observations(results))
                if f["control_id"] in control_ids and set(f["affected_restaurant_ids"]) & set(restaurants)]
    result_ids = {r.id for r in results}
    for finding in findings:
        finding["result_ids"] = [rid for rid in finding["result_ids"] if rid in result_ids]
        if "result_statuses" in finding:
            finding["result_statuses"] = {rid: state for rid, state in finding["result_statuses"].items() if rid in result_ids}
            finding["evaluation_status"] = aggregate_status(finding["result_statuses"].values())
        finding["in_scope_restaurant_ids"] = sorted(set(finding["affected_restaurant_ids"]) & set(restaurants))
        finding["overdue"] = (finding["status"] != "resolved" and bool(finding["due_date"]) and
                              finding["due_date"] < item.finished_at[:10]) if known_workflow else None
    active = [f for f in findings if f["status"] != "resolved"]
    summary = {**rollup(results), "findings": len(findings),
               "active_findings": len(active) if known_workflow else None,
               "resolved_findings": len(findings) - len(active) if known_workflow else None,
               "overdue_findings": sum(f["overdue"] for f in findings) if known_workflow else None,
               "severity_counts": {s: sum(f["severity"] == s for f in active) for s in SEVERITIES},
               "workflow_snapshot_available": known_workflow}
    counts = Counter(r.restaurant_id for r in results)
    assessed = sum(counts[rid] == len(controls) for rid in restaurants) if controls else 0
    total = item.policy["total_restaurants"]
    coverage = dict(total_restaurants=total, in_scope_restaurants=len(restaurants), assessed_restaurants=assessed,
                    unassessed_restaurants=total - assessed, out_of_scope_restaurants=total - len(restaurants))
    refs, serialized = {}, []
    at = datetime.fromisoformat(item.evaluated_at.replace("Z", "+00:00"))
    for result in results:
        result_refs = []
        for ref in db.scalars(select(ResultEvidence).where(ResultEvidence.result_id == result.id).order_by(ResultEvidence.evidence_id)):
            snap = ref.snapshot
            # Explicit allowlist: no file paths or source records in report output.
            entry = {k: snap.get(k) for k in ("id", "dataset_type", "sha256", "captured_at", "restaurant_ids")}
            try:
                age = round((at - datetime.fromisoformat(snap["captured_at"].replace("Z", "+00:00"))).total_seconds() / 86400, 3)
            except (ValueError, TypeError, KeyError):
                age = None
            entry.update(age_days_at_assessment=age, stale=age > item.policy["max_evidence_age_days"] if age is not None else None)
            refs[snap["id"]] = entry
            result_refs.append(snap["id"])
        serialized.append({key: getattr(result, key) for key in (
            "id", "restaurant_id", "control_id", "title", "rule_version", "status", "severity", "reason", "evaluated_at", "remediation", "framework_mappings")})
        serialized[-1]["evidence_ids"] = result_refs
    missing = []
    from app.evaluators import DEPENDENCIES
    for rid in restaurants:
        required = {"restaurants", "assets"} | {dataset for cid in control_ids for dataset in DEPENDENCIES[cid]} if control_ids else set()
        for dataset in sorted(required):
            if not item.evidence_manifest.get(rid, {}).get(dataset):
                missing.append({"restaurant_id": rid, "dataset_type": dataset})
    ages = [ref["age_days_at_assessment"] for ref in refs.values() if ref["age_days_at_assessment"] is not None]
    limitations = ["Automated evidence checks support review; they do not certify legal or PCI DSS compliance.",
                   "Coverage and evidence age describe the selected assessment; finding due dates are assessed at its completion time."]
    if not known_workflow:
        limitations.append("Historical workflow details were not captured. Findings show failure observations; active, resolved and overdue counts are unavailable.")
    data = dict(schema_version="1.0", report_id=f"REP-{uuid4().hex}", generated_at=stamp(), organization="MYL Inc.",
        assessment={"id": item.id, "status": item.status, "trigger": item.trigger, "created_at": item.created_at,
                    "started_at": item.started_at, "finished_at": item.finished_at, "evaluated_at": item.evaluated_at,
                    "scope": {"restaurant_ids": item.restaurant_ids, "framework_ids": item.framework_ids},
                    "rule_versions": {c["id"]: c["rule_version"] for c in controls}, "policy": item.policy},
        filters={"restaurant_id": restaurant_id, "framework_id": framework_id, "severity": severity},
        coverage=coverage, summary=summary,
        frameworks=[{"id": fid, **rollup([r for r in results if any(m["framework_id"] == fid for m in r.framework_mappings)])} for fid in frameworks],
        results=serialized, findings=findings, evidence_references=sorted(refs.values(), key=lambda ref: ref["id"]),
        evidence_health={"max_age_days": item.policy["max_evidence_age_days"], "oldest_age_days": max(ages) if ages else None,
                         "stale_references": sum(ref["stale"] is True for ref in refs.values()), "missing": missing,
                         "basis": "Age at assessment evaluation time; source hashes are the pinned snapshot references."},
        limitations=limitations)
    try:
        return ReportDocument.model_validate(data).model_dump(by_alias=True)
    except ValidationError:
        raise HTTPException(409, "This assessment has incomplete report metadata. Select another completed scan or run a new assessment.") from None


def create_reporting_router(database, current_session):
    router = APIRouter(prefix="/api", dependencies=[Depends(current_session)])

    @router.get("/dashboard")
    def dashboard(assessment_id: str | None = None, restaurant_id: str | None = None,
                  framework_id: Literal["GDPR", "PCI-DSS"] | None = None,
                  severity: Literal["critical", "high", "medium", "low"] | None = None, db=Depends(database)):
        history = list(db.scalars(select(Assessment).order_by(Assessment.created_at.desc(), text("assessments.rowid DESC")).limit(200)))
        latest_success = db.scalar(select(Assessment).where(Assessment.status == "completed").order_by(Assessment.created_at.desc(), text("assessments.rowid DESC")).limit(1))
        if assessment_id and not any(a.id == assessment_id for a in history):
            selected_item = db.get(Assessment, assessment_id)
            if selected_item:
                history.append(selected_item)
        selected = assessment_id or (latest_success.id if latest_success else None)
        return {"snapshot": report_data(db, selected, restaurant_id, framework_id, severity) if selected else None,
                "last_successful_scan": assessment_dict(latest_success) if latest_success else None,
                "latest_attempt": assessment_dict(history[0]) if history else None,
                "history": [assessment_dict(a) for a in history]}

    @router.get("/reports/{assessment_id}.yaml")
    def download(assessment_id: str, restaurant_id: str | None = None,
                 framework_id: Literal["GDPR", "PCI-DSS"] | None = None,
                 severity: Literal["critical", "high", "medium", "low"] | None = None, db=Depends(database)):
        data = report_data(db, assessment_id, restaurant_id, framework_id, severity)
        content = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110)
        # Filename uses server-generated identifiers only, never caller input.
        filename = f"myl-{data['report_id']}.yaml"
        return Response(content, media_type="application/yaml", headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    return router
