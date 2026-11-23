"""Check Phase 5 on the local app without resetting history or financial facts.

--schedule waits for a real one-minute interval (and restores saved settings).
--restart verifies persistence while that schedule is enabled. No business facts
are changed: the optional connector edit changes only its source-version label.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from dotenv import dotenv_values
import yaml

from smoke_phase4 import Client, SmokeFailure, check, financial_summary, fixture_hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schedule", action="store_true")
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    env = dotenv_values(root / ".env")
    api = Client(env.get("APP_ORIGIN") or "http://localhost:8080")
    identity = api.request("/auth/login", {"username": env.get("ADMIN_USERNAME") or "admin", "password": env["ADMIN_PASSWORD"]})
    api.csrf = identity["csrf_token"]
    financial_summary(api)
    fixtures = fixture_hashes(root)
    previous = {row["id"]: api.request(f"/assessments/{row['id']}") for row in api.rows("/assessments") if row["status"] in ("completed", "failed")}
    evidence_hashes = {row["id"]: row["sha256"] for row in api.rows("/evidence")}
    original = api.request("/schedule")
    fields = ("enabled", "interval_minutes", "restaurant_ids", "framework_ids")

    def save(values):
        revision = api.request("/schedule")["revision"]
        return api.request("/schedule", {**values, "expected_revision": revision}, method="PUT")

    def restart():
        subprocess.run(["docker", "compose", "restart", "backend"], cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                api.request("/ready")
                api.request("/auth/me")  # Cookie must survive restart.
                return
            except SmokeFailure:
                time.sleep(.3)
        raise SmokeFailure("Backend did not recover with the existing session")

    scheduled_id = None
    try:
        if args.schedule:
            save({**{key: original[key] for key in fields}, "enabled": False})
            saved = save(dict(enabled=True, interval_minutes=1, restaurant_ids=[f"R{i:03}" for i in range(1, 6)], framework_ids=["GDPR", "PCI-DSS"]))
            # First interval initializes the editable feed from the corrected
            # imports. A subsequent metadata-only change exercises collection.
            connector = root / "data" / "mock_sources" / "connector"
            if not connector.exists():
                deadline = time.monotonic() + 85
                while not connector.exists() and time.monotonic() < deadline:
                    time.sleep(.5)
                check(connector.exists(), "Scheduled connector was not initialized")
            source = connector / "payment.json"
            payload = json.loads(source.read_bytes())
            version = "myl-scheduled-collection-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            payload["source_version"] = version
            pending = source.with_suffix(".pending")
            pending.write_text(json.dumps(payload, indent=2) + "\n")
            pending.replace(source)
            if args.restart:
                before_restart = api.request("/schedule")
                restart()
                persisted = api.request("/schedule")
                for key in (*fields, "revision", "next_run_at", "last_assessment_id"):
                    check(persisted[key] == before_restart[key], "Schedule changed across restart")
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                state = api.request("/schedule")
                check(not state["last_error"], "Scheduled collection reported an error")
                last = state["last_assessment"]
                if last and last["status"] == "completed":
                    imports = api.request("/collections")["items"]
                    if any(row["source_version"] == version for row in imports):
                        scheduled_id = last["id"]
                        break
                time.sleep(.4)
            check(scheduled_id is not None, "Scheduled scan did not collect the source update")
        elif args.restart:
            restart()
        dashboard = api.request("/dashboard")
        selected = scheduled_id or (dashboard["snapshot"] or {}).get("assessment", {}).get("id")
        check(selected is not None, "Run an assessment before the Phase 5 smoke test")
        if not dashboard["snapshot"]["summary"]["workflow_snapshot_available"]:
            selected = api.scan()["id"]
        for filters in ({}, {"restaurant_id": "R003"}, {"framework_id": "GDPR"}, {"restaurant_id": "R002", "framework_id": "PCI-DSS", "severity": "high"}):
            parameters = urllib.parse.urlencode({"assessment_id": selected, **filters})
            snapshot = api.request("/dashboard?" + parameters)["snapshot"]
            url = api.origin + f"/api/reports/{selected}.yaml?" + urllib.parse.urlencode(filters)
            with api.opener.open(urllib.request.Request(url), timeout=10) as response:
                check(response.status == 200 and "application/yaml" in response.headers["Content-Type"], "YAML download failed")
                raw = response.read().decode()
            report = yaml.safe_load(raw)
            check(report["schema_version"] == "1.0", "Unexpected YAML schema")
            check(env["ADMIN_PASSWORD"] not in raw and "csrf_token" not in raw and "settings_json" not in raw, "Report contains excluded data")
            for key in ("assessment", "filters", "coverage", "summary", "frameworks", "results", "findings", "evidence_references"):
                check(report[key] == snapshot[key], "Dashboard and YAML disagree")
        for assessment_id, prior in previous.items():
            check(api.request(f"/assessments/{assessment_id}") == prior, "Earlier assessment history changed")
        current_evidence = {row["id"]: row["sha256"] for row in api.rows("/evidence")}
        check(all(current_evidence.get(key) == value for key, value in evidence_hashes.items()), "Earlier evidence references changed")
        check(fixture_hashes(root) == fixtures, "Committed seed fixtures changed")
        financial_summary(api)
        print(json.dumps({"phase": 5, "verified": True, "prior_assessments_preserved": len(previous), "selected_assessment": selected,
                          "scheduled_collection": bool(scheduled_id), "restart": args.restart, "report_filters_checked": 4}))
    finally:
        if args.schedule:
            save({key: original[key] for key in fields})


if __name__ == "__main__":
    try:
        main()
    except SmokeFailure as error:
        print(f"Phase 5 smoke failed: {error}", file=sys.stderr)
        raise SystemExit(1)
