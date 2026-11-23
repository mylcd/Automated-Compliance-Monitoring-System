"""Deterministic demo checks over pinned, validated source evidence snapshots."""
import json
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from app.contracts import SourceBundle, validate_rows
from app.seed import digest, safe_evidence_path

STATES = ("pass", "fail", "unknown", "not_applicable", "error")
DEPENDENCIES = {
    "LOY-01": ["loyalty_records", "processing_purposes", "consent_events", "customer_activities"],
    "LOY-02": ["loyalty_records", "processing_purposes", "consent_events", "customer_activities"],
    "LOY-03": ["loyalty_records", "processing_purposes"],
    "LOY-04": ["customer_requests"],
    "LOY-05": ["erp_access_approvals", "actual_access_grants", "employee_access_events", "employees", "restaurant_assignments"],
    **{f"PAY-0{i}": ["payment_configurations"] for i in range(1, 6)},
}
REMEDIATION = {
    "LOY-01": "Obtain and retain affirmative consent with purpose, time and notice version before consent-based processing.",
    "LOY-02": "Stop consent-based marketing after withdrawal; retain the withdrawal and any later valid re-consent.",
    "LOY-03": "Delete or anonymize overdue loyalty data according to the configured retention policy.",
    "LOY-04": "Review the overdue customer request and complete the response using its recorded due date.",
    "LOY-05": "Restrict observed permissions and successful actions to valid ERP approvals; investigate unauthorized changes.",
    "PAY-01": "Enable TLS and an approved protocol version on the affected payment system.",
    "PAY-02": "Apply the configured MFA and password policy to every active payment account.",
    "PAY-03": "Enable payment-system audit logging and verify the required events are actually recorded.",
    "PAY-04": "Install the version required by the configured patch policy and verify deployment.",
    "PAY-05": "Investigate the prohibited-storage indicator and remove prohibited sensitive authentication data.",
}


def instant(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timezone is required")
    return parsed.astimezone(timezone.utc)


class EvidenceIssue(Exception):
    def __init__(self, status, reason, details=None):
        self.status, self.reason = status, reason
        self.details = details or []
        super().__init__(reason)


class SnapshotContext:
    def __init__(self, settings, manifest, evaluated_at, max_age_days):
        self.settings, self.manifest = settings, manifest
        self.at = instant(evaluated_at)
        self.max_age_days = max_age_days
        self.cache = {}

    def load(self, dataset, restaurant):
        meta = self.manifest.get(restaurant, {}).get(dataset)
        if not meta:
            raise EvidenceIssue("unknown", f"No {dataset} evidence was collected for {restaurant}.")
        key = meta["id"]
        if key not in self.cache:
            try:
                path = safe_evidence_path(self.settings.evidence_dir, meta["relative_path"])
                raw = path.read_bytes()
                if digest(raw) != meta["sha256"]:
                    raise EvidenceIssue("error", f"{dataset} evidence failed SHA-256 integrity validation.")
                payload = json.loads(raw)
                if payload.get("dataset_type") != dataset or not isinstance(payload.get("records"), list):
                    raise EvidenceIssue("error", f"{dataset} snapshot has an invalid dataset or records structure.")
                source = SourceBundle.model_validate({k: payload[k] for k in ["schema_version", "source_type", "source_version", "captured_at", "restaurant_ids", "complete", "complete_months"]} | {"datasets": {dataset: payload["records"]}})
                if source.captured_at != meta["captured_at"] or source.restaurant_ids != meta["restaurant_ids"] or len(payload["records"]) != meta["record_count"]:
                    raise EvidenceIssue("error", f"{dataset} snapshot metadata does not match the pinned evidence.")
                rows = validate_rows(dataset, payload["records"])
                self.cache[key] = (source, rows)
            except FileNotFoundError:
                raise EvidenceIssue("unknown", f"{dataset} evidence file is unavailable.") from None
            except ValidationError as error:
                missing_only = all(item["type"] == "missing" for item in error.errors())
                raise EvidenceIssue("unknown" if missing_only else "error", f"{dataset} evidence contains {'missing required' if missing_only else 'malformed'} fields.") from None
            except (KeyError, ValueError, TypeError, OSError):
                raise EvidenceIssue("error", f"{dataset} evidence could not be safely validated.") from None
        source, rows = self.cache[key]
        if restaurant not in source.restaurant_ids or not source.complete or not meta["complete"] or meta["collection_status"] != "completed":
            raise EvidenceIssue("unknown", f"{dataset} evidence is incomplete for {restaurant}.")
        captured = instant(source.captured_at)
        if captured > self.at + timedelta(minutes=5):
            raise EvidenceIssue("error", f"{dataset} capture time is in the future.")
        if self.at - captured > timedelta(days=self.max_age_days):
            raise EvidenceIssue("unknown", f"{dataset} evidence exceeds the {self.max_age_days}-day demo freshness policy.")
        return rows

    def references(self, restaurant, datasets):
        found = {}
        for dataset in datasets:
            meta = self.manifest.get(restaurant, {}).get(dataset)
            if meta:
                found[meta["id"]] = {k: meta[k] for k in ["id", "dataset_type", "sha256", "captured_at", "restaurant_ids"]}
        return list(found.values())


def subject(row, kind, status, reason):
    return {"subject_id": row.get("id", row.get("asset_id", "dataset")), "subject_type": kind, "status": status, "reason": reason, **({"asset_id": row["asset_id"]} if "asset_id" in row else {})}


def outcome(details, empty_reason):
    if not details:
        return {"status": "not_applicable", "reason": empty_reason, "details": []}
    # A confirmed violation remains a failure even if another subject is unknown.
    # Uncertain subjects remain visible and can never turn an evaluation into pass.
    status = next((s for s in ["fail", "error", "unknown"] if any(d["status"] == s for d in details)), "pass")
    issues = [d for d in details if d["status"] == status]
    reason = f"{len(issues)} subject(s): {issues[0]['reason']}" if status != "pass" else f"All {len(details)} evaluated subject(s) meet the configured demo check."
    return {"status": status, "reason": reason, "details": details}


def consent_checks(control, restaurant, context, data, systems):
    records = {r["id"]: r for r in data["loyalty_records"]}
    purposes = {r["id"]: r for r in data["processing_purposes"]}
    events = data["consent_events"]
    details = []
    withdrawal_seen = False
    for activity in data["customer_activities"]:
        record = records.get(activity["loyalty_record_id"])
        if record is None:
            raise EvidenceIssue("unknown", "A processing activity has no supporting loyalty-record evidence.", details)
        if record["restaurant_id"] != restaurant or instant(activity["occurred_at"]) > context.at:
            continue
        purpose = purposes.get(record["purpose_id"])
        if purpose is None:
            raise EvidenceIssue("unknown", "A loyalty record has no supporting purpose evidence.", details)
        if purpose["lawful_basis"] != "consent":
            continue
        if activity["asset_id"] not in systems:
            raise EvidenceIssue("unknown", "A processing activity is outside the collected active loyalty-system scope.", details)
        history = [e for e in events if e["customer_id"] == record["customer_id"] and e["purpose_id"] == purpose["id"]]
        withdrawal_seen |= any(e["event_type"] == "withdraw" and instant(e["occurred_at"]) <= context.at for e in history)
        prior = [e for e in history if instant(e["occurred_at"]) <= instant(activity["occurred_at"])]
        prior.sort(key=lambda e: (instant(e["occurred_at"]), e["event_type"] == "withdraw"))
        last = prior[-1] if prior else None
        if control == "LOY-01":
            valid = bool(last and last["event_type"] == "grant" and last["notice_version"] and last["notice_version"].strip())
            details.append(subject(activity, "customer_activity", "pass" if valid else "fail", "Affirmative consent with notice evidence precedes this activity." if valid else "No valid affirmative consent with notice evidence at the time of this consent-based activity."))
        else:
            withdrawn = any(e["event_type"] == "withdraw" for e in prior)
            valid_reconsent = bool(last and last["event_type"] == "grant" and last["notice_version"] and last["notice_version"].strip())
            invalid = withdrawn and not valid_reconsent
            details.append(subject(activity, "customer_activity", "fail" if invalid else "pass", "Consent-based activity occurred after withdrawal without later re-consent." if invalid else "No consent-based activity after an effective withdrawal."))
    if control == "LOY-02" and not withdrawal_seen:
        return outcome([], "Complete collected evidence contains no relevant withdrawals to assess.")
    return outcome(details, "Complete collected evidence contains no consent-based processing activities to assess.")


def retention(restaurant, context, data):
    purposes = {r["id"]: r for r in data["processing_purposes"]}
    details = []
    for row in data["loyalty_records"]:
        if row["restaurant_id"] != restaurant or row["status"] != "active" or instant(row["collected_at"]) > context.at:
            continue
        purpose = purposes.get(row["purpose_id"])
        if purpose is None:
            raise EvidenceIssue("unknown", "Retention policy evidence is missing for a loyalty record.", details)
        if instant(row["retention_due_at"]) < instant(row["collected_at"]):
            raise EvidenceIssue("error", "A retention deadline precedes data collection.", details)
        deadline = min(instant(row["retention_due_at"]), instant(row["collected_at"]) + timedelta(days=purpose["retention_days"]))
        expired = context.at > deadline
        details.append(subject(row, "loyalty_record", "fail" if expired else "pass", "Active loyalty record exceeds its configured retention deadline." if expired else "Active loyalty record remains within its configured retention deadline."))
    return outcome(details, "Complete collected evidence contains no active loyalty records to assess.")


def requests(restaurant, context, data):
    details = []
    for row in data["customer_requests"]:
        if row["restaurant_id"] != restaurant or instant(row["received_at"]) > context.at:
            continue
        if instant(row["due_at"]) < instant(row["received_at"]) or (row["status"] == "completed" and not row["completed_at"]):
            raise EvidenceIssue("error", "Customer-request dates/status are inconsistent.", details)
        if row["completed_at"] and instant(row["completed_at"]) < instant(row["received_at"]):
            raise EvidenceIssue("error", "Request completion precedes receipt.", details)
        completed = row["status"] == "completed" and instant(row["completed_at"]) <= context.at
        overdue = not completed and instant(row["due_at"]) < context.at
        details.append(subject(row, "customer_request", "fail" if overdue else "pass", "Unresolved request is past its recorded due date." if overdue else "Request is completed or not yet past its recorded due date."))
    return outcome(details, "Complete collected evidence contains no received customer requests to assess.")


def access_checks(restaurant, context, data, systems):
    employees = {r["id"]: r for r in data["employees"]}
    assigned = {r["employee_id"] for r in data["restaurant_assignments"] if r["restaurant_id"] == restaurant}
    approvals = data["erp_access_approvals"]
    if any(a["asset_id"] in systems and a["valid_until"] is not None and instant(a["valid_until"]) <= instant(a["valid_from"]) for a in approvals):
        raise EvidenceIssue("error", "An approval has an invalid validity interval.")
    approved_through = instant(context.manifest[restaurant]["erp_access_approvals"]["captured_at"])

    def approved(employee, asset, permission, at, historical=False):
        if employee not in employees:
            raise EvidenceIssue("unknown", "Employee evidence is missing for an observed permission or action.")
        if at > approved_through:
            raise EvidenceIssue("unknown", "Approval evidence does not cover the observed action or permission time.")
        # Per-system approval plus the event's restaurant/system identifies its
        # historical scope. Current workplace assignments cannot rewrite history.
        return (historical or employee in assigned) and any(a["employee_id"] == employee and a["asset_id"] == asset and a["permission"] == permission and a["status"] == "approved" and instant(a["valid_from"]) <= at and (a["valid_until"] is None or at < instant(a["valid_until"])) for a in approvals)

    details = []
    observed_at = instant(context.manifest[restaurant]["actual_access_grants"]["captured_at"])
    for row in data["actual_access_grants"]:
        if row["asset_id"] not in systems or not row["active"]:
            continue
        try:
            valid = approved(row["employee_id"], row["asset_id"], row["permission"], observed_at)
            valid = valid and employees[row["employee_id"]]["active"]
        except EvidenceIssue as issue:
            details.append(subject(row, "access_grant", issue.status, issue.reason))
            continue
        details.append(subject(row, "access_grant", "pass" if valid else "fail", "Observed permission is within the approval valid at capture time." if valid else f"Observed {row['permission']} permission exceeds the employee's valid ERP approval/scope."))
    for row in data["employee_access_events"]:
        if row["restaurant_id"] != restaurant or instant(row["occurred_at"]) > context.at:
            continue
        if row["asset_id"] not in systems:
            details.append(subject(row, "employee_access_event", "unknown", "An employee action lacks matching active loyalty-system evidence."))
            continue
        if row["outcome"] == "denied":
            details.append(subject(row, "employee_access_event", "pass", "Denied action did not successfully modify customer data."))
            continue
        try:
            valid = approved(row["employee_id"], row["asset_id"], row["action"], instant(row["occurred_at"]), historical=True)
        except EvidenceIssue as issue:
            details.append(subject(row, "employee_access_event", issue.status, issue.reason))
            continue
        details.append(subject(row, "employee_access_event", "pass" if valid else "fail", "Successful action is within the approval valid at event time." if valid else f"Successful {row['action']} action exceeds the employee's ERP approval/scope at event time."))
    if not details:
        raise EvidenceIssue("unknown", "No observed permissions or action logs are available for the active loyalty system.")
    return outcome(details, "No access subjects to assess.")


def payment_checks(control, data, systems):
    configurations = {r["asset_id"]: r for r in data["payment_configurations"]}
    details = []
    for asset in systems:
        row = configurations.get(asset)
        if row is None:
            raise EvidenceIssue("unknown", f"Payment configuration evidence is missing for {asset}.", details)
        observed, policy = row["settings_json"], row["policy_json"]
        if control == "PAY-01":
            valid = observed["tls_enabled"] and observed["tls_version"] in policy["allowed_tls_versions"]
            reason = "TLS is enabled with an approved protocol." if valid else "TLS is disabled or its protocol is outside the configured allowed versions."
        elif control == "PAY-02":
            accounts = [a for a in observed["accounts"] if a["active"]]
            if not accounts:
                raise EvidenceIssue("unknown", "No active payment accounts are available to evaluate authentication.", details)
            for account in accounts:
                valid = (not policy["require_mfa"] or account["mfa_enabled"]) and account["password_length"] >= policy["minimum_password_length"]
                details.append(subject({**account, "asset_id": asset}, "payment_account", "pass" if valid else "fail", f"{asset}: active account meets the configured MFA/password policy." if valid else f"{asset}: active account violates the configured MFA/password policy."))
            continue
        elif control == "PAY-03":
            valid = observed["audit_logging_enabled"]
            reason = "Audit logging is enabled." if valid else "Payment-system audit logging is disabled."
        elif control == "PAY-04":
            valid = observed["installed_patch_version"] == policy["required_patch_version"]
            reason = "Installed patch matches the configured required version." if valid else "Installed patch does not match the configured required version."
        else:
            valid = not observed["prohibited_storage_detected"]
            reason = "Metadata reports no prohibited sensitive authentication-data storage." if valid else "Metadata indicates prohibited sensitive authentication-data storage."
        details.append(subject(row, "payment_system", "pass" if valid else "fail", reason))
    return outcome(details, "Complete inventory contains no active payment systems.")


def evaluate_control(control_id, restaurant_id, context):
    datasets = ["restaurants", "assets", *DEPENDENCIES[control_id]]
    evidence = context.references(restaurant_id, datasets)
    data = {}
    try:
        data = {name: context.load(name, restaurant_id) for name in datasets}
        restaurant = next((r for r in data["restaurants"] if r["id"] == restaurant_id), None)
        if restaurant is None:
            raise EvidenceIssue("unknown", "Restaurant is absent from its collected inventory evidence.")
        kind = "loyalty" if control_id.startswith("LOY") else "payment"
        systems = {a["id"] for a in data["assets"] if a["active"] and a["asset_type"] == kind and a["restaurant_id"] in [None, restaurant_id]}
        if not restaurant["active"] or not systems:
            result = outcome([], f"Complete inventory explicitly identifies an inactive restaurant or no active {kind} systems.")
        elif control_id in ["LOY-01", "LOY-02"]:
            result = consent_checks(control_id, restaurant_id, context, data, systems)
        elif control_id == "LOY-03":
            result = retention(restaurant_id, context, data)
        elif control_id == "LOY-04":
            result = requests(restaurant_id, context, data)
        elif control_id == "LOY-05":
            result = access_checks(restaurant_id, context, data, systems)
        else:
            result = payment_checks(control_id, data, systems)
    except EvidenceIssue as issue:
        result = outcome([*issue.details, subject({}, "evidence", issue.status, issue.reason)], "No subjects to assess.") if issue.details else {"status": issue.status, "reason": issue.reason, "details": []}
    except (ValueError, TypeError, KeyError, OverflowError):
        result = {"status": "error", "reason": "Evidence contains inconsistent values or unsupported relationships.", "details": []}
    shared = {a["id"] for a in data.get("assets", []) if a["restaurant_id"] is None}
    for detail in result["details"]:
        # Customer-specific processing remains a restaurant target even when its
        # application is shared; system/access configuration has shared identity.
        shared_subject = detail.get("asset_id") in shared and detail["subject_type"] in ["payment_system", "payment_account", "access_grant", "employee_access_event"]
        detail["target_type"] = "shared_system" if shared_subject else "restaurant"
        detail["target_id"] = detail["asset_id"] if shared_subject else restaurant_id
    return {**result, "evidence": evidence, "remediation": REMEDIATION[control_id]}
