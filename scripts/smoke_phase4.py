"""Verify Phase 4 on the deployed local app without resetting demo data.

Run with the same Python environment as smoke_test.py. Credentials come only
from the repository's private .env. --restart also verifies Docker persistence.
This deliberately changes runtime evidence through the two named corrections.
"""

import argparse
from datetime import date, timedelta
import hashlib
import http.cookiejar
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from dotenv import dotenv_values


RESTAURANTS = [f"R{i:03}" for i in range(1, 6)]
FRAMEWORKS = ["GDPR", "PCI-DSS"]
TARGETS = {("R001", "LOY-01"), ("R002", "PAY-01"), ("R003", "PAY-03")}
STATUSES = ("open", "in_progress", "awaiting_verification", "resolved")
FINANCIALS = {
    "total_payments_minor": 68588172,
    "total_sales_minor": 62352913,
    "total_tax_minor": 6235259,
    "total_payroll_minor": 22875000,
}


class SmokeFailure(Exception):
    """A diagnostic safe to print without response bodies or secrets."""


def check(condition, message):
    if not condition:
        raise SmokeFailure(message)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def fixture_hashes(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((root / "backend" / "seed_data").rglob("*.json"))
    }


class Client:
    def __init__(self, origin):
        self.origin = origin.rstrip("/")
        self.csrf = None
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def request(self, path, payload=None, method=None, expected=200):
        headers = {"X-Requested-With": "XMLHttpRequest", "Origin": self.origin}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        method = method or ("POST" if payload is not None else "GET")
        request = urllib.request.Request(
            self.origin + "/api" + path, data=data, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=10) as response:
                check(response.status == expected, "Unexpected API success status")
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            if status == expected:
                return None
            # Never include response bodies, request payloads, or identity data.
            raise SmokeFailure(f"{method} {path.split('?')[0]} returned HTTP {status}") from None
        except (OSError, urllib.error.URLError):
            raise SmokeFailure("Local API connection failed") from None

    def rows(self, path, **filters):
        items, offset = [], 0
        while True:
            query = urllib.parse.urlencode({**filters, "limit": 200, "offset": offset})
            page = self.request(path + "?" + query)
            check(isinstance(page.get("items"), list), "Missing API page items")
            items.extend(page["items"])
            check(len(items) <= page["total"], "API pagination total changed")
            if len(items) == page["total"]:
                return items
            check(bool(page["items"]), "API pagination stopped before total")
            offset += len(page["items"])

    def detail(self, finding_id):
        item = self.request(f"/findings/{finding_id}")
        required = {"owner_employee_id", "due_date", "notes", "status", "revision",
                    "resolved_result_id", "correction_actions", "history", "result"}
        check(required <= item.keys(), "Finding detail lacks Phase 4 fields")
        check(item["status"] in STATUSES, "Unknown finding lifecycle status")
        check(type(item["revision"]) is int and item["revision"] >= 0,
              "Finding revision must be a nonnegative integer")
        check(isinstance(item["notes"], str), "Finding notes must be text")
        if item["due_date"] is not None:
            check(date.fromisoformat(item["due_date"]).isoformat() == item["due_date"],
                  "Finding due date must be an ISO date")
        return item

    def scan(self, restaurants=None, frameworks=None):
        restaurants = RESTAURANTS if restaurants is None else restaurants
        frameworks = FRAMEWORKS if frameworks is None else frameworks
        accepted = self.request("/assessments", {
            "restaurant_ids": restaurants, "framework_ids": frameworks,
        }, expected=202)
        deadline = time.monotonic() + 60
        while True:
            run = self.request(f"/assessments/{accepted['id']}")
            if run["status"] not in ("queued", "running"):
                break
            check(time.monotonic() < deadline, "Assessment did not finish within 60 seconds")
            time.sleep(0.2)
        check(run["status"] == "completed", "Assessment failed")
        total = len(restaurants) * (5 * len(frameworks))
        check(run["completed_evaluations"] == run["total_evaluations"] == total,
              "Assessment evaluation count is incorrect")
        check(run["results"] == self.rows(f"/assessments/{run['id']}/results"),
              "Assessment detail and result page disagree")
        return run


def check_full_scan(run, final=False):
    failures = {(r["restaurant_id"], r["control_id"]) for r in run["results"]
                if r["status"] == "fail"}
    check(("R003", "PAY-03") in failures and failures <= TARGETS,
          "Full assessment failures differ from the bounded demo scenario")
    check(len(run["results"]) == 50, "Full assessment must contain 50 results")
    counts = {"pass": 50 - len(failures), "fail": len(failures), "unknown": 0,
              "error": 0, "not_applicable": 0}
    check(run["summary"]["counts"] == counts, "Unexpected full assessment counts")
    check(run["summary"]["pass_rate"] == counts["pass"] * 2,
          "Unexpected full assessment pass rate")
    check(run["summary"]["coverage"] == {
        "total_restaurants": 5, "in_scope_restaurants": 5, "assessed_restaurants": 5,
    }, "Unexpected full assessment coverage")
    if final:
        check(failures == {("R003", "PAY-03")} and counts["pass"] == 49,
              "Corrected full assessment must be 49 pass / 1 fail / 98 percent")


def financial_summary(api):
    summary = api.request("/resource-planning/summary?month=2026-10")
    check(summary["complete"] is True and summary["tax_rate_percent"] == 10,
          "October financial summary is incomplete")
    check(len(summary["totals"]) == 1 and summary["totals"][0]["currency"] == "USD",
          "Expected one USD financial total")
    total = summary["totals"][0]
    check(all(total[key] == value for key, value in FINANCIALS.items()),
          "October gross/net/tax/payroll baseline changed")
    check(len(summary["restaurants"]) == 5, "Financial summary must cover five restaurants")
    for key in FINANCIALS:
        check(sum(row["totals"][0][key] for row in summary["restaurants"]) == total[key],
              "Restaurant financial totals do not reconcile")
    return summary


def check_latest_scan(api, run, prior_restaurants=None):
    overview = api.request("/overview")
    latest = overview["latest_assessment"]
    check(latest["id"] == run["id"] and latest["status"] == "completed",
          "Overview last scan does not identify the completed assessment")
    for field in ("summary", "started_at", "finished_at", "evaluated_at"):
        check(latest[field] == run[field] and latest[field] is not None,
              "Overview last scan fields disagree with assessment history")
    check(overview["assessed_restaurants"] == 5, "Assessed restaurant coverage changed")
    restaurants = {row["id"]: row for row in api.request("/restaurants")["items"]}
    for restaurant in RESTAURANTS:
        expected = (run["id"] if restaurant in run["restaurant_ids"]
                    else prior_restaurants[restaurant]["latest_assessment_id"])
        check(restaurants[restaurant]["assessment_status"] == "assessed"
              and restaurants[restaurant]["latest_assessment_id"] == expected,
              "Restaurant last scan escaped the requested assessment scope")
        detail = api.request(f"/restaurants/{restaurant}")
        check(detail["latest_assessment_id"] == expected,
              "Restaurant detail last scan disagrees with list")
    return restaurants


def snapshot_history(api):
    history, failed, evidence = {}, {}, {}
    for row in api.rows("/assessments"):
        if row["status"] != "completed":
            continue
        run = api.request(f"/assessments/{row['id']}")
        check(run["results"] == api.rows(f"/assessments/{row['id']}/results"),
              "Historical assessment result page disagrees with detail")
        history[row["id"]] = run
        for result in run["results"]:
            if result["status"] == "fail":
                check(api.request(f"/results/{result['id']}") == result,
                      "Historical failing result detail disagrees")
                failed[result["id"]] = result
            for ref in result["evidence"]:
                if ref["id"] not in evidence:
                    evidence[ref["id"]] = fingerprint(
                        api.request(f"/evidence/{ref['id']}/content")
                    )
    return history, failed, evidence


def check_history(api, snapshot):
    history, failed, evidence = snapshot
    for run_id, run in history.items():
        check(api.request(f"/assessments/{run_id}") == run,
              "A historical assessment or its matched subjects changed")
        check(api.rows(f"/assessments/{run_id}/results") == run["results"],
              "Historical assessment results changed")
    for result_id, result in failed.items():
        check(api.request(f"/results/{result_id}") == result,
              "A historical baseline failure or matched subject changed")
    for evidence_id, digest in evidence.items():
        check(fingerprint(api.request(f"/evidence/{evidence_id}/content")) == digest,
              "Previously pinned immutable evidence changed")


def audit_events(api, finding_id):
    events = api.rows("/audit-events", finding_id=finding_id)
    check(bool(events), "Finding audit trail is empty")
    for event in events:
        check({"actor", "event_type", "before", "after", "finding_id", "id"} <= event.keys(),
              "Audit event lacks required actor/type/before/after fields")
        check(bool(event["actor"]) and bool(event["event_type"]),
              "Audit event actor or event type is empty")
        check(event["finding_id"] == finding_id, "Audit finding filter leaked another finding")
        check(isinstance(event["before"], dict) and isinstance(event["after"], dict),
              "Audit before/after must be structured objects")
    return {event["id"]: event for event in events}


def correction(api, finding_id, action, owner):
    finding = api.detail(finding_id)
    previews = [item for item in finding["correction_actions"] if item["action"] == action]
    check(len(previews) == 1, "Expected named correction preview is missing or duplicated")
    preview = previews[0]
    check(type(preview["already_applied"]) is bool and isinstance(preview["changes"], list),
          "Correction preview lacks applied state or changes")
    for change in preview["changes"]:
        check({"dataset", "subject_id", "field", "before", "after"} <= change.keys(),
              "Correction preview lacks field-level before/after changes")
    if not preview["already_applied"]:
        check(any(change["before"] != change["after"] for change in preview["changes"]),
              "Unapplied correction preview has no effective changes")
        check(finding["status"] != "resolved", "Resolved finding still requires source correction")
        update = {"owner_employee_id": owner,
                  "due_date": (date.today() + timedelta(days=7)).isoformat(),
                  "notes": "Phase 4 smoke verification", "status": "in_progress"}
        if any(finding.get(key) != value for key, value in update.items()):
            previous_revision = finding["revision"]
            finding = api.request(f"/findings/{finding_id}", {
                **update, "expected_revision": previous_revision,
            }, method="PATCH")
            check(all(finding[key] == value for key, value in update.items()),
                  "Finding assignment/due date/notes/status PATCH did not persist")
            check(type(finding["revision"]) is int and finding["revision"] > previous_revision,
                  "Finding PATCH did not advance revision")
            check(api.detail(finding_id) == finding, "PATCH did not return persisted full detail")

    before_evidence = {row["id"]: row for row in api.rows("/evidence")}
    result = api.request(f"/findings/{finding_id}/corrections", {
        "action": action, "expected_revision": finding["revision"],
    })
    check({"finding", "changed", "revision_id"} <= result.keys(),
          "Correction response lacks finding/changed/revision_id")
    check(type(result["changed"]) is bool and result["changed"] != preview["already_applied"],
          "Correction changed flag disagrees with preview")
    current = api.detail(finding_id)
    check(result["finding"] == current, "Correction did not return persisted full detail")
    after_evidence = {row["id"]: row for row in api.rows("/evidence")}
    check(all(after_evidence.get(key) == row for key, row in before_evidence.items()),
          "Correction modified existing immutable evidence metadata")
    if result["changed"]:
        check(current["status"] == "awaiting_verification" and current["resolved_result_id"] is None,
              "Source correction resolved a finding before verification")
        check(current["revision"] > finding["revision"] and bool(result["revision_id"]),
              "Correction did not record a revision")
        new_ids = after_evidence.keys() - before_evidence.keys()
        dataset = "consent_events" if action == "restore_consent_evidence" else "payment_configurations"
        check(any(after_evidence[key]["dataset_type"] == dataset for key in new_ids),
              "Correction did not import new immutable evidence for the affected source")
    else:
        check(current == finding and after_evidence == before_evidence,
              "An already applied correction changed finding or evidence state")

    # Reapply with the current revision: no new evidence, revisions or transitions.
    repeated = api.request(f"/findings/{finding_id}/corrections", {
        "action": action, "expected_revision": current["revision"],
    })
    check(repeated["changed"] is False and repeated["finding"] == current,
          "Repeated correction is not idempotent")
    check(api.detail(finding_id) == current, "Repeated correction changed persisted lifecycle state")
    check({row["id"]: row for row in api.rows("/evidence")} == after_evidence,
          "Repeated correction imported duplicate evidence")
    applied = next(item for item in current["correction_actions"] if item["action"] == action)
    check(applied["already_applied"] is True, "Correction preview still reports unapplied source")
    audit_events(api, finding_id)
    return int(result["changed"])


def check_resolution(api, finding_id, restaurant, control, expected_run=None):
    finding = api.detail(finding_id)
    check(finding["status"] == "resolved" and bool(finding["resolved_result_id"]),
          "Passing verification did not resolve the finding")
    result = api.request(f"/results/{finding['resolved_result_id']}")
    check((result["restaurant_id"], result["control_id"], result["status"])
          == (restaurant, control, "pass"), "Resolution does not reference the matching passing result")
    if expected_run:
        check(result["assessment_id"] == expected_run, "Resolution references the wrong verification scan")
    check(finding["latest_evaluation_status"] == "pass", "Resolved finding latest evaluation is not pass")
    return finding


def check_final_findings(api, ids):
    expected = {ids[("R001", "LOY-01")]: "resolved", ids[("R002", "PAY-01")]: "resolved",
                ids[("R003", "PAY-03")]: "open"}
    default = {row["id"]: row["status"] for row in api.rows("/findings")}
    explicit = {row["id"]: row["status"] for row in api.rows("/findings", status="all")}
    check(default == explicit == expected, "Default/all findings must retain two resolved and one open finding")
    for status in STATUSES:
        check({row["id"] for row in api.rows("/findings", status=status)}
              == {key for key, value in expected.items() if value == status},
              "Finding status filter returned the wrong set")
    check_resolution(api, ids[("R001", "LOY-01")], "R001", "LOY-01")
    check_resolution(api, ids[("R002", "PAY-01")], "R002", "PAY-01")
    remaining = api.detail(ids[("R003", "PAY-03")])
    check(remaining["latest_evaluation_status"] == "fail" and remaining["resolved_result_id"] is None,
          "Uncorrected R003 finding no longer references a failure")
    return {finding_id: api.detail(finding_id) for finding_id in expected}


def restart_backend(api, root):
    try:
        completed = subprocess.run(["docker", "compose", "restart", "backend"],
                                   cwd=root, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise SmokeFailure("Docker backend restart could not complete") from None
    check(completed.returncode == 0, "Docker backend restart failed")
    deadline = time.monotonic() + 60
    while True:
        try:
            if api.request("/ready")["status"] == "ready":
                return
        except SmokeFailure:
            pass
        check(time.monotonic() < deadline, "Backend did not recover after restart")
        time.sleep(0.5)


def verify(api, root, restart):
    originals = fixture_hashes(root)
    summary = financial_summary(api)
    history = snapshot_history(api)
    findings = api.rows("/findings", status="all")
    if not findings:
        check_full_scan(api.scan())
        findings = api.rows("/findings", status="all")
        history = snapshot_history(api)
    ids = {(row["restaurant_id"], row["control_id"]): row["id"] for row in findings}
    check(len(findings) == 3 and set(ids) == TARGETS,
          "Expected the three historical demo findings; no fixtures were reset")
    first, second, third = (ids[("R001", "LOY-01")], ids[("R002", "PAY-01")], ids[("R003", "PAY-03")])
    restaurant_scans = {row["id"]: row for row in api.request("/restaurants")["items"]}
    changed = correction(api, first, "restore_consent_evidence", "E001-01")
    before_selective = {finding_id: api.detail(finding_id) for finding_id in (first, second, third)}
    selective = api.scan(["R001"], ["GDPR"])
    check(selective["summary"]["counts"] == {
        "pass": 5, "fail": 0, "unknown": 0, "error": 0, "not_applicable": 0,
    }, "Selective R001 GDPR verification must pass all five controls")
    check_resolution(api, first, "R001", "LOY-01",
                     selective["id"] if before_selective[first]["status"] != "resolved" else None)
    for finding_id in (second, third):
        check(api.detail(finding_id) == before_selective[finding_id],
              "Selective R001 GDPR scan changed an out-of-scope finding")
    check_latest_scan(api, selective, restaurant_scans)
    changed += correction(api, second, "enable_tls", "E002-01")
    # No assessment runs between this check and the full verification scan.
    before_full = api.detail(second)
    final = api.scan()
    check_full_scan(final, final=True)
    check_resolution(api, second, "R002", "PAY-01",
                     final["id"] if before_full["status"] != "resolved" else None)
    final_findings = check_final_findings(api, ids)
    check_latest_scan(api, final)
    corrected_summary = financial_summary(api)
    check(corrected_summary["totals"] == summary["totals"] and corrected_summary["restaurants"] == summary["restaurants"], "Corrections changed October financial amounts")
    # Recollection advances capture times/evidence IDs, while all amounts stay fixed.
    summary = corrected_summary
    check_history(api, history)
    audits = {finding_id: audit_events(api, finding_id) for finding_id in ids.values()}

    if restart:
        persistent_evidence = {row["id"]: row for row in api.rows("/evidence")}
        persistent_sources = api.request("/collections")
        restart_backend(api, root)
        check(api.request("/auth/me")["csrf_token"] == api.csrf,
              "Session or CSRF state did not survive backend restart")
        check(check_final_findings(api, ids) == final_findings,
              "Finding assignments, lifecycle or revisions did not survive restart")
        check(financial_summary(api) == summary, "Financial state or sources changed after restart")
        check({row["id"]: row for row in api.rows("/evidence")} == persistent_evidence,
              "Immutable evidence metadata did not survive restart")
        check(api.request("/collections") == persistent_sources,
              "Imported source collection history changed after restart")
        check(api.request(f"/assessments/{final['id']}") == final,
              "Latest verification results did not survive restart")
        check_latest_scan(api, final)
        for finding_id, previous in audits.items():
            current = audit_events(api, finding_id)
            check(all(current.get(key) == event for key, event in previous.items()),
                  "Finding audit history did not survive restart")
        # A fresh scan catches startup re-imports of original, uncorrected sources.
        final = api.scan()
        check_full_scan(final, final=True)
        check_final_findings(api, ids)
        check_latest_scan(api, final)
        check_history(api, history)
        check(financial_summary(api) == summary, "Financial summary changed after restart verification")

    check(fixture_hashes(root) == originals, "Original seed fixtures changed during verification")
    return len(history[0]), changed, final["id"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restart", action="store_true", help="Restart only the Docker backend and verify persistence")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = dotenv_values(root / ".env")
    check(bool(config.get("ADMIN_USERNAME")) and bool(config.get("ADMIN_PASSWORD")),
          "Private .env must contain ADMIN_USERNAME and ADMIN_PASSWORD")
    api = Client(config.get("APP_ORIGIN") or "http://localhost:8080")
    check(api.request("/ready")["status"] == "ready", "Backend is not ready")
    api.request("/findings", expected=401)
    api.request("/audit-events", expected=401)
    identity = api.request("/auth/login", {
        "username": config["ADMIN_USERNAME"], "password": config["ADMIN_PASSWORD"],
    })
    api.csrf = identity["csrf_token"]
    try:
        check(identity["role"] == "General Manager", "General Manager session required")
        count, changed, assessment_id = verify(api, root, args.restart)
        check(api.request("/auth/me")["username"] == config["ADMIN_USERNAME"],
              "Authenticated session identity changed")
    finally:
        failing = sys.exc_info()[0] is not None
        try:
            api.request("/auth/logout", {}, expected=204)
        except Exception:
            if not failing:
                raise
    api.csrf = None
    api.request("/findings", expected=401)
    print(f"PASS: Phase 4; prior_runs={count}; corrections_changed={changed}; "
          f"49 pass / 1 fail / 98%; findings=2 resolved, 1 open; latest={assessment_id}")
    print("PASS: scoped verification, idempotency, audit, immutable history/evidence, October financials, session/logout"
          + (", Docker restart and corrected runtime sources" if args.restart else ""))


if __name__ == "__main__":
    try:
        main()
    except SmokeFailure as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
    except Exception:
        # Unexpected API shapes and configuration errors must not dump secrets.
        print("FAIL: Unexpected response shape or local configuration error; no credentials were printed", file=sys.stderr)
        sys.exit(1)
