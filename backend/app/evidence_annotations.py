"""Display-only failure locations derived from immutable assessment evidence."""
import json

from fastapi import HTTPException
from sqlalchemy import select, text

from app import domain as d
from app.assessment_models import Assessment, AssessmentResult, ResultEvidence
from app.seed import digest, safe_evidence_path
from app.evaluators import instant


def annotations(db, settings, evidence_id, result_id=None, assessment_id=None,
                restaurant_id=None, framework_id=None, severity=None):
    evidence = db.get(d.Evidence, evidence_id)
    if evidence is None:
        raise HTTPException(404, "Evidence not found")
    query = select(AssessmentResult).join(ResultEvidence).where(ResultEvidence.evidence_id == evidence_id)
    if result_id:
        query = query.where(AssessmentResult.id == result_id)
    if assessment_id:
        query = query.where(AssessmentResult.assessment_id == assessment_id)
    if not result_id and not assessment_id:
        latest = db.scalar(select(Assessment).join(AssessmentResult).join(ResultEvidence)
            .where(ResultEvidence.evidence_id == evidence_id, Assessment.status == "completed")
            .order_by(Assessment.created_at.desc(), text("assessments.rowid DESC")).limit(1))
        if latest:
            query = query.where(AssessmentResult.assessment_id == latest.id)
        else:
            return {"assessment_id": None, "result_ids": [], "issues": [], "highlights": []}
    results = list(db.scalars(query.order_by(AssessmentResult.restaurant_id, AssessmentResult.control_id)))
    if not results:
        raise HTTPException(404, "This evidence is not linked to the selected assessment result")
    context = db.get(Assessment, results[0].assessment_id)
    results = [r for r in results if (not restaurant_id or r.restaurant_id == restaurant_id)
               and (not severity or r.severity == severity)
               and (not framework_id or any(m["framework_id"] == framework_id for m in r.framework_mappings))]
    cache, highlights, issues = {}, [], []

    def load(result, dataset):
        refs = list(db.scalars(select(ResultEvidence).where(ResultEvidence.result_id == result.id)))
        ref = next((r for r in refs if r.snapshot.get("dataset_type") == dataset), None)
        if not ref:
            return []
        if ref.evidence_id not in cache:
            item = db.get(d.Evidence, ref.evidence_id)
            try:
                raw = safe_evidence_path(settings.evidence_dir, item.relative_path).read_bytes()
                if digest(raw) != ref.snapshot["sha256"] or item.sha256 != ref.snapshot["sha256"]:
                    raise HTTPException(409, "Pinned assessment evidence failed integrity validation")
                payload = json.loads(raw)
                if payload["dataset_type"] != dataset or not isinstance(payload["records"], list):
                    raise ValueError()
                cache[ref.evidence_id] = payload["records"]
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                raise HTTPException(409, "Pinned assessment evidence is unavailable for highlighting") from None
        return cache[ref.evidence_id]

    def mark(index, fields, issue, suffix=None):
        base = f"/records/{index}" + (suffix or "")
        for field in fields or [None]:
            path = base + ("/" + field if field else "")
            item = {"path": path, "reason": issue["reason"], "subject_id": issue["subject_id"],
                    "control_id": issue["control_id"], "restaurant_id": issue["restaurant_id"]}
            if item not in highlights:
                highlights.append(item)

    for result in results:
        rows = load(result, evidence.dataset_type)
        for subject in result.details:
            if subject.get("status") != "fail":
                continue
            issue = {"result_id": result.id, "control_id": result.control_id, "restaurant_id": result.restaurant_id,
                     "subject_id": subject["subject_id"], "reason": subject["reason"], "missing": False}
            if result.control_id in ["LOY-01", "LOY-02"]:
                activities = load(result, "customer_activities")
                activity = next((r for r in activities if r["id"] == subject["subject_id"]), None)
                if not activity:
                    continue
                record = next((r for r in load(result, "loyalty_records") if r["id"] == activity["loyalty_record_id"]), None)
                if not record:
                    continue
                issue.update(customer_id=record["customer_id"], purpose_id=record["purpose_id"], activity_at=activity["occurred_at"])
                matching = [r for r in load(result, "consent_events") if r["customer_id"] == record["customer_id"] and r["purpose_id"] == record["purpose_id"]]
                prior = sorted([r for r in matching if instant(r["occurred_at"]) <= instant(activity["occurred_at"])],
                               key=lambda r: (instant(r["occurred_at"]), r["event_type"] == "withdraw"))
                latest = prior[-1] if prior else None
                if evidence.dataset_type == "customer_activities":
                    for i, row in enumerate(rows):
                        if row["id"] == activity["id"]:
                            mark(i, [], issue)
                elif evidence.dataset_type == "consent_events":
                    issue["missing"] = latest is None or latest["event_type"] != "grant"
                    issue["explanation"] = (f"No supporting consent grant for {record['customer_id']} / {record['purpose_id']} at the time of {activity['id']} ({activity['occurred_at']}). "
                        "A missing record cannot be highlighted in the JSON; unrelated customers' events are unchanged.") if issue["missing"] else "The recorded grant has no notice-version evidence."
                    candidates = [latest] if latest else sorted([r for r in matching if r["event_type"] == "grant"], key=lambda r: instant(r["occurred_at"]))[:1]
                    for i, row in enumerate(rows):
                        if row in candidates:
                            fields = (["notice_version"] if instant(row["occurred_at"]) <= instant(activity["occurred_at"]) else ["occurred_at"]) if row["event_type"] == "grant" else ["event_type", "occurred_at"]
                            mark(i, fields, issue)
            elif result.control_id == "LOY-03" and evidence.dataset_type == "loyalty_records":
                for i, row in enumerate(rows):
                    if row["id"] == subject["subject_id"]:
                        mark(i, ["status", "retention_due_at"], issue)
            elif result.control_id == "LOY-04" and evidence.dataset_type == "customer_requests":
                for i, row in enumerate(rows):
                    if row["id"] == subject["subject_id"]:
                        mark(i, ["status", "due_at", "completed_at"], issue)
            elif result.control_id == "LOY-05":
                dataset = "actual_access_grants" if subject["subject_type"] == "access_grant" else "employee_access_events"
                bad = next((r for r in load(result, dataset) if r["id"] == subject["subject_id"]), None)
                if bad:
                    issue.update(employee_id=bad["employee_id"], asset_id=bad["asset_id"])
                    if evidence.dataset_type == dataset:
                        for i, row in enumerate(rows):
                            if row["id"] == bad["id"]:
                                mark(i, ["permission", "active"] if dataset == "actual_access_grants" else ["action", "outcome", "occurred_at"], issue)
                    elif evidence.dataset_type == "erp_access_approvals":
                        issue["missing"] = True
                        issue["explanation"] = f"No valid approval/scope supported {bad['employee_id']}'s observed permission or successful action on {bad['asset_id']} at the evaluation's relevant time. Existing approved permissions are supporting context, not incorrect records."
                    elif evidence.dataset_type == "employees" and dataset == "actual_access_grants":
                        for i, row in enumerate(rows):
                            if row["id"] == bad["employee_id"] and not row["active"]:
                                mark(i, ["active"], issue)
            elif result.control_id.startswith("PAY-") and evidence.dataset_type == "payment_configurations":
                for i, row in enumerate(rows):
                    if row["asset_id"] != subject.get("asset_id", subject["subject_id"]):
                        continue
                    observed, policy = row["settings_json"], row["policy_json"]
                    fields = []
                    if result.control_id == "PAY-01":
                        if not observed["tls_enabled"]:
                            fields.append("tls_enabled")
                        if observed["tls_version"] not in policy["allowed_tls_versions"]:
                            fields.append("tls_version")
                    elif result.control_id == "PAY-02":
                        for j, account in enumerate(observed["accounts"]):
                            if account["id"] != subject["subject_id"]:
                                continue
                            if policy["require_mfa"] and not account["mfa_enabled"]:
                                mark(i, ["mfa_enabled"], issue, f"/settings_json/accounts/{j}")
                            if account["password_length"] < policy["minimum_password_length"]:
                                mark(i, ["password_length"], issue, f"/settings_json/accounts/{j}")
                    else:
                        fields = [{"PAY-03": "audit_logging_enabled", "PAY-04": "installed_patch_version", "PAY-05": "prohibited_storage_detected"}[result.control_id]]
                    if fields:
                        mark(i, fields, issue, "/settings_json")
            if issue not in issues:
                issues.append(issue)
    return {"assessment_id": context.id, "evaluated_at": context.evaluated_at, "result_ids": [r.id for r in results],
            "issues": issues, "highlights": highlights}
