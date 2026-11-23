"""Snapshot/report consistency and deterministic interval monitoring tests."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
from threading import Event, Thread
import time

import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient
from sqlalchemy import func, select
import yaml

from app.assessment_models import Assessment, AssessmentResult, Finding
from app.assessments import AssessmentInput
from app.main import create_app
from app.monitoring import ScanScheduler, connector_path, fingerprint, publish_connector
from app.monitoring_models import ScanSchedule
from app.remediation import source_bundles
from app.reporting import ReportDocument
from app.seed import import_sources, load_sources
from test_phase3 import (NOW, PASSWORD, HEADERS, RESTAURANTS, FRAMEWORKS, client, factory,
                         settings, seeded as base_seeded, scan, write_headers)
from test_phase4 import correct, detail, patch, files, financial_and_visit_records

REAL_LOOP = ScanScheduler.loop


@pytest.fixture
def clock():
    return [NOW]


@pytest.fixture
def seeded(base_seeded, monkeypatch, clock):
    from app import assessments, monitoring, remediation
    for module in (assessments, monitoring, remediation):
        monkeypatch.setattr(module, "utcnow", lambda: clock[0])
    # Most tests drive due ticks explicitly. One test below exercises the real loop.
    monkeypatch.setattr(ScanScheduler, "loop", lambda self: self.stop_event.wait())
    return base_seeded


def snapshot(client, assessment=None, **filters):
    response = client.get("/api/dashboard", params={**({"assessment_id": assessment["id"]} if assessment else {}), **filters})
    assert response.status_code == 200, response.text
    return response.json()["snapshot"]


def save_schedule(client, **changes):
    current = client.get("/api/schedule").json()
    payload = {key: current[key] for key in ("enabled", "interval_minutes", "restaurant_ids", "framework_ids")}
    payload.update(expected_revision=current["revision"], **changes)
    response = client.put("/api/schedule", json=payload, headers=write_headers(client))
    assert response.status_code == 200, response.text
    return response.json()


def due(client, clock, minutes=1):
    clock[0] += timedelta(minutes=minutes)
    return client.app.state.scheduler.tick(clock[0])


def export(client, assessment, **filters):
    response = client.get(f"/api/reports/{assessment['id']}.yaml", params=filters)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/yaml")
    assert response.headers["content-disposition"].endswith('.yaml"')
    data = yaml.safe_load(response.text)
    assert ReportDocument.model_validate(data).model_dump(by_alias=True) == data
    assert "!!python" not in response.text
    return data


@pytest.mark.parametrize("filters", [{}, {"restaurant_id": "R002"}, {"framework_id": "GDPR"},
    {"restaurant_id": "R002", "framework_id": "PCI-DSS", "severity": "high"}, {"severity": "low"}])
def test_dashboard_yaml_and_finding_filters_share_one_snapshot(client, filters):
    assessment, results = scan(client)
    data = snapshot(client, assessment, **filters)
    report = export(client, assessment, **filters)
    for key in ("assessment", "filters", "coverage", "summary", "frameworks", "results", "findings", "evidence_references", "evidence_health"):
        assert report[key] == data[key], key
    selected = [r for r in results if (not filters.get("restaurant_id") or r["restaurant_id"] == filters["restaurant_id"]) and
                (not filters.get("severity") or r["severity"] == filters["severity"]) and
                (not filters.get("framework_id") or any(m["framework_id"] == filters["framework_id"] for m in r["framework_mappings"]))]
    assert {r["id"] for r in data["results"]} == {r["id"] for r in selected}
    assert data["summary"]["total_evaluations"] == len(selected)
    assert data["coverage"]["total_restaurants"] == 5
    assert data["coverage"]["assessed_restaurants"] + data["coverage"]["unassessed_restaurants"] == 5
    for finding in data["findings"]:
        assert set(finding["result_ids"]) <= {r["id"] for r in selected}


def test_workflow_and_reports_stay_frozen_after_new_scans_and_assignments(client, clock):
    first, _ = scan(client)
    before = snapshot(client, first)
    finding = client.get("/api/findings", params={"control_id": "PAY-01"}).json()["items"][0]
    assigned = patch(client, finding, owner_employee_id="E002-01", due_date="2026-11-16", notes="private operator note")
    second, _ = scan(client)
    newer = snapshot(client, second)
    assert newer["summary"]["overdue_findings"] == 1
    assert next(f for f in newer["findings"] if f["id"] == finding["id"])["owner_employee_id"] == "E002-01"
    corrected = correct(client, detail(client, assigned), "enable_tls")["finding"]
    third, _ = scan(client)
    latest = snapshot(client, third)
    assert latest["summary"]["active_findings"] == 2
    assert latest["summary"]["resolved_findings"] == 1
    assert latest["summary"]["overdue_findings"] == 0
    for data in (snapshot(client, first), export(client, first)):
        assert data["assessment"] == before["assessment"]
        assert data["summary"] == before["summary"]
        assert data["findings"] == before["findings"]
    assert "private operator note" not in json.dumps(export(client, third))
    assert corrected["status"] == "awaiting_verification"


def test_report_contains_only_allowed_metadata_not_source_rows_or_secrets(client, seeded):
    assessment, _ = scan(client)
    data = export(client, assessment)
    text = json.dumps(data)
    for forbidden in (PASSWORD, "csrf_token", "password_hash", "settings_json", "payment_received_minor", "salary_minor", "relative_path"):
        assert forbidden not in text
    assert data["schema_version"] == "1.0"
    assert data["evidence_references"]
    assert all(len(e["sha256"]) == 64 and e["age_days_at_assessment"] >= 0 for e in data["evidence_references"])


def test_scoped_coverage_no_data_failed_and_unknown_states(client, seeded, factory, monkeypatch):
    assert client.get("/api/dashboard").json()["snapshot"] is None
    assessment, _ = scan(client, restaurants=["R001"], frameworks=["GDPR"])
    data = snapshot(client, assessment)
    assert data["coverage"] == dict(total_restaurants=5, in_scope_restaurants=1, assessed_restaurants=1, unassessed_restaurants=4, out_of_scope_restaurants=4)
    assert client.get("/api/dashboard", params={"assessment_id": assessment["id"], "restaurant_id": "R005"}).status_code == 422
    assert client.get(f"/api/reports/{assessment['id']}.yaml", params={"framework_id": "PCI-DSS"}).status_code == 422
    assert client.get("/api/reports/missing.yaml").status_code == 404
    with factory.begin() as db:
        row = db.get(Assessment, assessment["id"])
        row.findings_snapshot = None
    legacy = snapshot(client, assessment)
    assert legacy["summary"]["active_findings"] is None
    assert legacy["summary"]["overdue_findings"] is None
    assert all(f["status"] == "unrecorded" for f in legacy["findings"])
    from app import assessments
    monkeypatch.setattr(assessments, "evaluate_control", lambda *args: (_ for _ in ()).throw(RuntimeError("test")))
    queued = client.post("/api/assessments", json={"restaurant_ids": ["R001"], "framework_ids": ["GDPR"]}, headers=write_headers(client)).json()
    failed = client.get("/api/dashboard").json()
    assert failed["latest_attempt"]["status"] == "failed"
    assert failed["last_successful_scan"]["id"] == assessment["id"]
    assert client.get(f"/api/reports/{queued['id']}.yaml").status_code == 409


def test_missing_and_stale_evidence_remain_unknown_in_report(client, seeded, factory, clock):
    from app import domain as d
    with factory.begin() as db:
        item = db.scalar(select(d.Evidence).where(d.Evidence.dataset_type == "payment_configurations"))
        (seeded.evidence_dir / item.relative_path).unlink()
    assessment, _ = scan(client)
    data = snapshot(client, assessment)
    assert data["summary"]["counts"]["unknown"] == 25
    assert data["summary"]["pass_rate"] == 48
    assert data["summary"]["applicable_evaluations"] == 50
    clock[0] += timedelta(days=40)
    old, _ = scan(client)
    data = export(client, old)
    assert data["summary"]["counts"]["unknown"] == 50
    assert data["evidence_health"]["stale_references"] > 0


def test_schedule_defaults_validation_csrf_and_version_conflicts(client):
    current = client.get("/api/schedule").json()
    assert not current["enabled"] and current["interval_minutes"] == 60 and current["next_run_at"] is None
    payload = dict(enabled=True, interval_minutes=1, restaurant_ids=RESTAURANTS, framework_ids=FRAMEWORKS, expected_revision=0)
    assert client.put("/api/schedule", json=payload, headers=HEADERS).status_code == 403
    for invalid in (0, 10081, 1.5, "1"):
        assert client.put("/api/schedule", json={**payload, "interval_minutes": invalid}, headers=write_headers(client)).status_code == 422
    assert client.put("/api/schedule", json={**payload, "restaurant_ids": []}, headers=write_headers(client)).status_code == 422
    saved = save_schedule(client, enabled=True, interval_minutes=1)
    assert saved["enabled"] and saved["next_run_at"]
    assert client.put("/api/schedule", json=payload, headers=write_headers(client)).status_code == 409
    assert save_schedule(client, enabled=False)["next_run_at"] is None


def test_scheduled_collection_detects_changed_mock_source_without_manual_scan(client, seeded, factory, clock):
    original_facts = financial_and_visit_records(factory)
    original_files = files(seeded.evidence_dir)
    save_schedule(client, enabled=True, interval_minutes=1)
    first_id = due(client, clock)
    first = snapshot(client)
    assert first["assessment"]["id"] == first_id and first["summary"]["pass_rate"] == 94
    path = connector_path(seeded) / "payment.json"
    payload = json.loads(path.read_bytes())
    next(r for r in payload["datasets"]["payment_configurations"] if r["asset_id"] == "R002-payment")["settings_json"]["tls_enabled"] = True
    path.write_text(json.dumps(payload))
    second_id = due(client, clock)
    second = snapshot(client)
    assert first_id != second_id and second["summary"]["pass_rate"] == 96
    assert second["assessment"]["trigger"] == "scheduled"
    assert next(f for f in second["findings"] if f["control_id"] == "PAY-01")["status"] == "resolved"
    assert snapshot(client, {"id": first_id})["summary"] == first["summary"]
    assert financial_and_visit_records(factory) == original_facts
    assert all((seeded.evidence_dir / path).read_bytes() == contents for path, contents in original_files.items())
    state = client.get("/api/schedule").json()
    assert state["last_assessment_id"] == second_id and state["last_run_at"] and state["next_run_at"] and not state["last_error"]


def test_manual_overlap_defers_due_scan_without_collecting_or_losing_due_time(client, seeded, clock):
    service = client.app.state.assessment_service
    queued = service.queue(AssessmentInput(restaurant_ids=RESTAURANTS, framework_ids=FRAMEWORKS), "admin")
    saved = save_schedule(client, enabled=True, interval_minutes=1)
    assert due(client, clock) is None
    assert not connector_path(seeded).exists()
    assert client.get("/api/schedule").json()["next_run_at"] == saved["next_run_at"]
    assert client.get("/api/schedule").json()["last_error"] is None
    service.run(queued["id"])
    accepted = client.app.state.scheduler.tick(clock[0])
    assert accepted and accepted != queued["id"]
    assert client.app.state.scheduler.tick(clock[0]) is None


def test_connector_failure_is_atomic_and_retryable(client, seeded, factory, clock):
    with factory() as db:
        publish_connector(seeded, source_bundles(db, seeded))
    path = connector_path(seeded) / "payment.json"
    original = path.read_bytes()
    path.write_text('{"bad":')
    before = files(seeded.evidence_dir)
    save_schedule(client, enabled=True, interval_minutes=1)
    assert due(client, clock) is None
    assert files(seeded.evidence_dir) == before
    assert client.get("/api/assessments").json()["total"] == 0
    assert client.get("/api/schedule").json()["last_error"]
    assert client.get("/api/audit-events", params={"event_type": "schedule_collection_failed"}).json()["total"] == 1
    path.write_bytes(original)
    assert due(client, clock)
    assert client.get("/api/schedule").json()["last_error"] is None


def test_connector_keeps_intervening_corrections_and_detects_conflicts(client, seeded, factory, clock):
    save_schedule(client, enabled=True, interval_minutes=1)
    due(client, clock)
    finding = client.get("/api/findings", params={"control_id": "PAY-01"}).json()["items"][0]
    correct(client, finding, "enable_tls")
    assert due(client, clock)
    assert snapshot(client)["summary"]["pass_rate"] == 96
    with factory() as db:
        state = json.loads((connector_path(seeded) / ".state.json").read_bytes())
        assert state["base_fingerprint"] == fingerprint(source_bundles(db, seeded))
    # Simultaneously editing the feed and importing a different source generation
    # must produce a visible collection error, never silently undo either edit.
    incoming = load_sources(connector_path(seeded))
    imported = deepcopy(incoming)
    imported["payment"]["source_version"] += "-external"
    import_sources(seeded, imported)
    path = connector_path(seeded) / "payment.json"
    feed = json.loads(path.read_bytes())
    feed["source_version"] += "-pending"
    path.write_text(json.dumps(feed))
    before = path.read_bytes()
    assert due(client, clock) is None
    assert path.read_bytes() == before
    assert client.get("/api/schedule").json()["last_error"]


def test_settings_and_missed_interval_recover_after_restart(seeded, clock):
    with TestClient(create_app(seeded)) as first:
        first.post("/api/auth/login", json={"username": "admin", "password": PASSWORD}, headers=HEADERS)
        saved = save_schedule(first, enabled=True, interval_minutes=1)
        cookies = first.cookies
    clock[0] += timedelta(minutes=10)
    with TestClient(create_app(seeded)) as restarted:
        restarted.cookies.update(cookies)
        persisted = restarted.get("/api/schedule").json()
        assert persisted["enabled"] and persisted["next_run_at"] == saved["next_run_at"]
        assert restarted.app.state.scheduler.tick(clock[0])
        assert restarted.app.state.scheduler.tick(clock[0]) is None
        assert restarted.get("/api/assessments").json()["total"] == 1  # No catch-up burst.


def test_real_background_loop_runs_without_api_trigger(seeded, monkeypatch, clock):
    monkeypatch.setattr(ScanScheduler, "loop", REAL_LOOP)
    with TestClient(create_app(seeded)) as live:
        live.post("/api/auth/login", json={"username": "admin", "password": PASSWORD}, headers=HEADERS)
        save_schedule(live, enabled=True, interval_minutes=1)
        clock[0] += timedelta(minutes=1)
        live.app.state.scheduler.wake_event.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            rows = live.get("/api/assessments").json()["items"]
            if rows and rows[0]["status"] == "completed":
                break
            Event().wait(.05)
        assert len(rows) == 1 and rows[0]["status"] == "completed" and rows[0]["trigger"] == "scheduled"


def test_only_one_scheduler_owner_can_recover_this_database(client, seeded):
    scheduler = ScanScheduler(seeded, None, None)
    with pytest.raises(RuntimeError, match="one MYL backend"):
        scheduler.acquire()


def test_reset_requires_disabled_schedule(client, seeded):
    from app.seed import reset_demo
    save_schedule(client, enabled=True, interval_minutes=1)
    with pytest.raises(ValueError, match="Disable scheduled"):
        reset_demo(seeded)


def test_versioned_schema_matches_export_and_rejects_extra_source_fields(client):
    schema = Path(__file__).resolve().parents[1] / "schemas" / "report-v1.schema.json"
    assert json.loads(schema.read_text()) == ReportDocument.model_json_schema(by_alias=True)
    assessment, _ = scan(client)
    data = export(client, assessment)
    data["results"][0]["settings_json"] = {"forbidden": True}
    with pytest.raises(ValidationError):
        ReportDocument.model_validate(data)


def test_not_applicable_is_excluded_and_never_marked_as_passing_finding(client, seeded):
    scan(client)
    bundles = load_sources()
    next(asset for asset in bundles["erp"]["datasets"]["assets"] if asset["id"] == "R002-payment")["active"] = False
    import_sources(seeded, bundles)
    assessment, _ = scan(client)
    data = export(client, assessment)
    assert data["summary"]["counts"]["not_applicable"] == 5
    assert data["summary"]["applicable_evaluations"] == 45
    assert data["summary"]["pass_rate"] == round(43 / 45 * 100, 2)
    finding = next(f for f in data["findings"] if f["control_id"] == "PAY-01")
    assert finding["status"] == "open" and finding["evaluation_status"] == "not_applicable"
    filtered = snapshot(client, assessment, restaurant_id="R002", framework_id="PCI-DSS")
    assert filtered["summary"]["pass_rate"] is None


def test_missing_pin_is_named_in_evidence_health(client, factory):
    from app import domain as d
    with factory.begin() as db:
        evidence = db.scalar(select(d.Evidence).where(d.Evidence.dataset_type == "payment_configurations"))
        evidence.restaurant_ids = ["R002", "R003", "R004", "R005"]
    assessment, _ = scan(client)
    data = export(client, assessment)
    assert {"restaurant_id": "R001", "dataset_type": "payment_configurations"} in data["evidence_health"]["missing"]
    assert data["summary"]["counts"]["unknown"] == 5


def test_collection_rolls_back_files_and_import_if_reservation_fails_after_import(client, seeded, factory, clock, monkeypatch):
    from app import domain as d
    with factory() as db:
        publish_connector(seeded, source_bundles(db, seeded))
        original_sources = list(db.scalars(select(d.CollectionRun.id)))
    source = connector_path(seeded) / "payment.json"
    payload = json.loads(source.read_bytes())
    payload["source_version"] += "-pending-update"
    source.write_text(json.dumps(payload))
    prior_files = files(seeded.evidence_dir)
    service = client.app.state.assessment_service
    real_queue = service.queue

    def failed_queue(payload, username, **kwargs):
        callback = kwargs["on_queued"]
        def fail(db, item):
            callback(db, item)
            raise RuntimeError("Failure after connector import and queue flush")
        return real_queue(payload, username, **{**kwargs, "on_queued": fail})

    monkeypatch.setattr(service, "queue", failed_queue)
    save_schedule(client, enabled=True, interval_minutes=1)
    assert due(client, clock) is None
    assert files(seeded.evidence_dir) == prior_files
    with factory() as db:
        assert list(db.scalars(select(d.CollectionRun.id))) == original_sources
        assert db.scalar(select(func.count()).select_from(Assessment)) == 0
    monkeypatch.setattr(service, "queue", real_queue)
    assert due(client, clock)
