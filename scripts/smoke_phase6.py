"""Full rehearsal on the disposable 127.0.0.1:8081 copy only.

Requires --reset-demo. Never mounts or resets the primary application's data.
Reports and a verification record are retained outside the repository.
"""
import argparse
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import sys
import time
import urllib.request

from dotenv import dotenv_values
import yaml

from demo import ROOT, DEMO, ORIGIN, compose, backend_python, reset_demo, set_tls
from smoke_phase4 import Client, SmokeFailure, check, financial_summary, fingerprint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset-demo", action="store_true", help="Reset the isolated presentation copy before rehearsal")
    args = parser.parse_args()
    if not args.reset_demo:
        parser.error("Pass --reset-demo to rehearse against a clean isolated copy")
    check(ORIGIN == "http://127.0.0.1:8081", "Unexpected rehearsal destination")
    reset_demo()
    env = dotenv_values(ROOT / ".env")
    api = Client(ORIGIN)
    api.request("/system", expected=401)
    api.request("/dashboard", expected=401)
    api.request("/schedule", expected=401)
    identity = api.request("/auth/login", {"username": env.get("ADMIN_USERNAME") or "admin", "password": env["ADMIN_PASSWORD"]})
    api.csrf = identity["csrf_token"]
    artifacts = DEMO / "rehearsal-artifacts"
    artifacts.mkdir(exist_ok=True)
    stages = []

    def verify(run, rate, failures):
        check(run["summary"]["pass_rate"] == rate, "Unexpected rehearsal pass rate")
        check(run["summary"]["counts"]["fail"] == failures, "Unexpected failure count")
        check(len(run["results"]) == 50 and run["summary"]["coverage"]["assessed_restaurants"] == 5, "Full assessment scope changed")
        snapshot = api.request("/dashboard?assessment_id=" + run["id"])["snapshot"]
        with api.opener.open(ORIGIN + f"/api/reports/{run['id']}.yaml", timeout=10) as response:
            raw = response.read()
        report = yaml.safe_load(raw)
        for key in ("assessment", "coverage", "summary", "results", "findings", "evidence_references"):
            check(report[key] == snapshot[key], "Selected report differs from dashboard")
        check(env["ADMIN_PASSWORD"].encode() not in raw and b"csrf_token" not in raw, "Export contains excluded data")
        (artifacts / f"stage-{len(stages)+1}-{int(rate)}.yaml").write_bytes(raw)
        stages.append({"assessment_id": run["id"], "trigger": run["trigger"], "pass_rate": rate, "failures": failures})
        return snapshot

    try:
        overview = api.request("/overview")
        check((overview["restaurants"], overview["employees"], overview["visits"], overview["assessed_restaurants"]) == (5, 61, 3375, 0), "Clean seeded inventory differs")
        financial_summary(api)
        check(api.request("/visits/days?month=2026-10&limit=200")["total"] == 155, "Daily visit grouping differs")
        baseline = api.scan()
        baseline_snapshot = verify(baseline, 92, 4)
        extra = next(r for r in baseline["results"] if r["restaurant_id"] == "R001" and r["control_id"] == "LOY-05")
        check(extra["status"] == "fail", "Presentation LOY-05 case was not detected")
        check(any(s["subject_id"] == "EV-UNAUTHORIZED-001" and s["status"] == "fail" for s in extra["details"]), "Unauthorized Manager update is missing")
        initial = {(f["restaurant_id"], f["control_id"]): f for f in api.rows("/findings")}
        repeat = api.scan()
        check({f["id"] for f in api.rows("/findings")} == {f["id"] for f in initial.values()}, "Repeat scan duplicated findings")
        for key, action, owner, rate, failures in (
            (("R001", "LOY-01"), "restore_consent_evidence", "E001-01", 94, 3),
            (("R002", "PAY-01"), "enable_tls", "E002-01", 96, 2)):
            finding = api.detail(initial[key]["id"])
            evidence = finding["result"]["evidence"]
            check(bool(evidence), "Failure lacks evidence references")
            for ref in evidence:
                content = api.request(f"/evidence/{ref['id']}/content")
                check(content["schema_version"] == "1.0", "Evidence content is unavailable")
            assigned = api.request(f"/findings/{finding['id']}", {
                "owner_employee_id": owner, "due_date": (date.today()+timedelta(days=7)).isoformat(),
                "notes": "Phase 6 rehearsal", "status": "in_progress", "expected_revision": finding["revision"]}, method="PATCH")
            changed = api.request(f"/findings/{finding['id']}/corrections", {"action": action, "expected_revision": assigned["revision"]})
            check(changed["finding"]["status"] == "awaiting_verification", "Correction resolved before re-scan")
            verified = api.scan()
            verify(verified, rate, failures)
            check(api.detail(finding["id"])["status"] == "resolved", "Passing re-scan did not resolve target")
        previous_results = baseline["results"]
        check(api.request(f"/assessments/{baseline['id']}")["results"] == previous_results, "Baseline results were overwritten")
        check(api.request("/dashboard?assessment_id=" + baseline["id"])["snapshot"]["findings"] == baseline_snapshot["findings"], "Baseline workflow snapshot changed")
        old_evidence = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (DEMO / "evidence").rglob("*.json")}

        def schedule(enabled):
            current = api.request("/schedule")
            return api.request("/schedule", {"enabled": enabled, "interval_minutes": 1,
                "restaurant_ids": [f"R{i:03}" for i in range(1, 6)], "framework_ids": ["GDPR", "PCI-DSS"],
                "expected_revision": current["revision"]}, method="PUT")

        def wait_scheduled(previous_id):
            deadline = time.monotonic() + 85
            while time.monotonic() < deadline:
                state = api.request("/schedule")
                check(not state["last_error"], "Scheduled source collection failed")
                run = state["last_assessment"]
                if run and run["id"] != previous_id and run["status"] == "completed":
                    return api.request(f"/assessments/{run['id']}")
                time.sleep(.3)
            raise SmokeFailure("Scheduled assessment did not complete within its interval")

        set_tls(False)
        schedule(True)
        recurrent = wait_scheduled(None)
        verify(recurrent, 94, 3)
        tls = api.detail(initial[("R002", "PAY-01")]["id"])
        check(tls["status"] == "open" and tls["reopen_count"] == 1, "Recurring failure did not reopen the original finding")
        set_tls(True)
        restored = wait_scheduled(recurrent["id"])
        verify(restored, 96, 2)
        schedule(False)
        check(api.detail(tls["id"])["status"] == "resolved", "Scheduled passing evidence did not resolve recurrence")
        check(all(p.exists() and hashlib.sha256(p.read_bytes()).hexdigest() == sha for p, sha in old_evidence.items()), "Scheduled collection changed prior evidence files")

        # Simulate a process restart after reservation but before execution.
        queued = backend_python('''
from app.config import Settings
from app.database import create_database_engine, session_factory
from app.assessments import AssessmentInput, AssessmentService
s=Settings(); e=create_database_engine(s.database_url)
item=AssessmentService(s, session_factory(e)).queue(AssessmentInput(restaurant_ids=["R001"], framework_ids=["GDPR"]), "rehearsal")
print(item["id"])
e.dispose()
''', capture=True).stdout.strip()
        compose("restart", "backend", capture=True)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            try:
                api.request("/ready")
                api.request("/auth/me")
                break
            except SmokeFailure:
                time.sleep(.3)
        interrupted = api.request(f"/assessments/{queued}")
        check(interrupted["status"] == "failed" and interrupted["results"] == [], "Restart did not recover the interrupted reservation")
        retry = api.request(f"/assessments/{queued}/retry", {}, expected=202)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            recovered = api.request(f"/assessments/{retry['id']}")
            if recovered["status"] == "completed":
                break
            time.sleep(.2)
        check(recovered["status"] == "completed" and recovered["summary"]["pass_rate"] == 100, "Retry failed to recover the interrupted scope")
        # Finish on a full-company result, avoiding a scoped 100% presentation.
        final = api.scan()
        verify(final, 96, 2)
        events = {row["event_type"] for row in api.rows("/audit-events")}
        check({"finding_assigned", "source_changed", "finding_resolved", "finding_reopened", "schedule_triggered", "assessment_interrupted"} <= events, "Expected audit events are missing")
        financial_summary(api)
        api.request("/auth/logout", {}, method="POST", expected=204)
        api.request("/system", expected=401)
        # Re-seed the isolated copy and prove baseline repeatability.
        reset_demo()
        identity = api.request("/auth/login", {"username": env.get("ADMIN_USERNAME") or "admin", "password": env["ADMIN_PASSWORD"]})
        api.csrf = identity["csrf_token"]
        repeated_baseline = api.scan()
        verify(repeated_baseline, 92, 4)
        check(api.request("/findings")["total"] == 4, "Reset did not reproduce baseline findings")
        financial_summary(api)
        record = {"phase": 6, "verified": True, "origin": ORIGIN, "stages": stages,
                  "repeat_scan_deduplication": True, "immutable_baseline": True,
                  "scheduled_reopen_and_resolution": True, "restart_recovery_and_retry": True,
                  "login_logout_denial": True, "financial_totals_unchanged": True, "reset_reproduced_baseline": True}
        (artifacts / "verification.json").write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps(record))
    finally:
        try:
            current = api.request("/schedule")
            if current["enabled"]:
                api.request("/schedule", {"enabled": False, "interval_minutes": 60,
                    "restaurant_ids": current["restaurant_ids"], "framework_ids": current["framework_ids"],
                    "expected_revision": current["revision"]}, method="PUT")
        except SmokeFailure:
            pass


if __name__ == "__main__":
    try:
        main()
    except SmokeFailure as error:
        print(f"Phase 6 rehearsal failed: {error}", file=sys.stderr)
        raise SystemExit(1)
