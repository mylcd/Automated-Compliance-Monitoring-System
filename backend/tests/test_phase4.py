"""Phase 4 remediation, source-history, verification, and audit regressions.

All mutations use temporary databases/evidence/runtime sources. Both service
clocks are frozen together: a correction must never appear to come from the
future to the assessment evaluator.
"""

from collections import Counter
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, Table, func, inspect, select, text

from app import domain as d
from app.assessment_models import Assessment, AssessmentResult, Finding, ResultEvidence
from app.bootstrap import BACKEND_DIR, bootstrap
from app.database import create_database_engine, session_factory
from app.main import COOKIE_NAME, create_app, hash_token
from app.models import LoginSession, User
from app.seed import FIXTURES, import_sources, load_sources, reset_demo

# Pytest also discovers these fixtures and their dependencies.
from test_phase3 import (
    BASELINE_FAILURES,
    HEADERS,
    NOW,
    PASSWORD,
    RESTAURANTS,
    client,
    factory,
    findings,
    keyed,
    rewrite_snapshot,
    scan,
    seeded as phase3_seeded,
    settings,
    write_headers,
)


@pytest.fixture
def seeded(phase3_seeded, monkeypatch):
    from app import remediation

    monkeypatch.setattr(remediation, "utcnow", lambda: NOW)
    return phase3_seeded


@pytest.fixture
def baseline(client):
    assessment, results = scan(client)
    rows = keyed(findings(client)["items"])
    assert set(rows) == BASELINE_FAILURES
    assert assessment["summary"]["pass_rate"] == 94
    return assessment, results, rows


def detail(client, finding):
    finding_id = finding if isinstance(finding, str) else finding["id"]
    response = client.get(f"/api/findings/{finding_id}")
    assert response.status_code == 200, response.text
    return response.json()


def patch(client, finding, **changes):
    response = client.patch(
        f"/api/findings/{finding['id']}",
        json={"expected_revision": finding["revision"], **changes},
        headers=write_headers(client),
    )
    assert response.status_code == 200, response.text
    return response.json()


def correct(client, finding, action):
    response = client.post(
        f"/api/findings/{finding['id']}/corrections",
        json={"action": action, "expected_revision": finding["revision"]},
        headers=write_headers(client),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["finding"] == detail(client, finding)
    return body


def audit(client, **params):
    response = client.get("/api/audit-events", params={"limit": 200, **params})
    assert response.status_code == 200, response.text
    return response.json()


def files(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def database_snapshot(factory):
    """Capture committed values, including source pointers and audit rows."""
    with factory() as db:
        connection = db.connection()
        metadata = MetaData()
        return {
            name: sorted(
                (dict(row) for row in connection.execute(select(Table(name, metadata, autoload_with=connection))).mappings()),
                key=lambda row: json.dumps(row, sort_keys=True),
            )
            for name in inspect(connection).get_table_names()
        }


def counts(factory):
    from app.remediation_models import MockSourceRevision

    with factory() as db:
        return tuple(db.scalar(select(func.count()).select_from(model)) for model in (MockSourceRevision, d.CollectionRun, d.Evidence))


def financial_and_visit_records(factory):
    """Evidence pointers may advance; the underlying financial facts may not."""
    with factory() as db:
        result = {}
        for name in ("employee_payroll", "payroll_allocations", "sales_transactions", "visits"):
            model = d.DATASETS[name]
            columns = [column for column in model.__table__.columns if column.name != "evidence_id"]
            result[name] = sorted(
                (dict(row) for row in db.execute(select(*columns)).mappings()),
                key=lambda row: json.dumps(row, sort_keys=True),
            )
        return result


def revision_directory(settings, factory, revision_id):
    from app.remediation_models import MockSourceRevision

    with factory() as db:
        revision = db.get(MockSourceRevision, revision_id)
        assert revision is not None
        path = (settings.runtime_source_dir / revision.relative_path).resolve()
        assert path.is_relative_to(settings.runtime_source_dir.resolve())
        assert revision.created_by == "admin"
        assert revision.finding_id
        assert revision.action in {"restore_consent_evidence", "enable_tls"}
        assert instant(revision.created_at) == NOW
        manifest_hash = revision.sha256
    # The persisted path may identify the directory or its manifest.
    directory = path if path.is_dir() else path.parent
    manifest_bytes = (directory / "manifest.json").read_bytes()
    assert hashlib.sha256(manifest_bytes).hexdigest() == manifest_hash
    manifest = json.loads(manifest_bytes)
    assert manifest == {
        name: hashlib.sha256(contents).hexdigest()
        for name, contents in source_files(directory).items()
    }
    return directory


def source_files(directory):
    expected = {"erp.json", "loyalty.json", "payment.json"} | {
        f"visits/{rid}-2026-10.json" for rid in RESTAURANTS
    }
    result = {name: (directory / name).read_bytes() for name in expected}
    assert {str(path.relative_to(directory)) for path in directory.rglob("*.json")} == expected | {"manifest.json"}
    return result


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_upgrade_0004_preserves_existing_history_credentials_and_evidence(settings):
    """Insert through reflected 0004 tables, never through the new Finding ORM."""
    from app.remediation_models import AuditEvent, MockSourceRevision

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.attributes["database_url"] = settings.database_url
    command.upgrade(config, "0004")
    engine = create_database_engine(settings.database_url)
    legacy_factory = session_factory(engine)
    try:
        with legacy_factory.begin() as db:
            db.add(User(id=1, username="legacy-admin", password_hash="keep-this-hash"))
            db.flush()
            db.add(LoginSession(token_hash="legacy-token", user_id=1, csrf_token="legacy-csrf", expires_at=9999999999))
        import_sources(settings, load_sources())
        at = NOW.isoformat()
        with engine.begin() as connection:
            metadata = MetaData()
            tables = {name: Table(name, metadata, autoload_with=connection) for name in ("assessments", "assessment_results", "result_evidence", "findings")}
            connection.execute(tables["assessments"].insert(), dict(
                id="A-LEGACY", status="completed", active_slot=None, trigger="manual", requested_by="legacy-admin",
                restaurant_ids=["R002"], framework_ids=["PCI-DSS"], created_at=at, started_at=at, finished_at=at,
                evaluated_at=at, total_evaluations=1, completed_evaluations=1, error=None,
                policy={"legacy": True}, controls_snapshot=[{"id": "PAY-01"}], evidence_manifest={}, summary={"legacy": 1},
            ))
            connection.execute(tables["assessment_results"].insert(), dict(
                id="RES-LEGACY", assessment_id="A-LEGACY", restaurant_id="R002", control_id="PAY-01",
                title="Legacy TLS failure", rule_version="1", status="fail", severity="high", reason="TLS disabled",
                evaluated_at=at, framework_mappings=[], details=[{"subject_id": "R002-payment", "status": "fail"}], remediation="Enable TLS",
            ))
            evidence_id = connection.execute(select(d.Evidence.id).where(d.Evidence.dataset_type == "payment_configurations")).scalar_one()
            connection.execute(tables["result_evidence"].insert(), dict(result_id="RES-LEGACY", evidence_id=evidence_id, snapshot={"id": evidence_id}))
            connection.execute(tables["findings"].insert(), dict(
                id="F-LEGACY", restaurant_id="R002", target_type="restaurant", target_id="R002",
                affected_restaurant_ids=["R002"], control_id="PAY-01", title="Legacy TLS failure", severity="high",
                status="open", first_seen_at=at, last_seen_at=at, occurrences=4,
                first_result_id="RES-LEGACY", last_result_id="RES-LEGACY", reason="TLS disabled", remediation="Enable TLS",
            ))
        previous = database_snapshot(legacy_factory)
        evidence_files = files(settings.evidence_dir)
        bootstrap(settings)
        current = database_snapshot(legacy_factory)
        for table, rows in previous.items():
            if table == "assessments":
                assert [{key: row[key] for key in rows[0]} for row in current[table]] == rows
                assert all(row["findings_snapshot"] is None for row in current[table])
            elif table not in {"alembic_version", "findings"}:
                assert current[table] == rows, table
        migrated = current["findings"][0]
        assert {key: migrated[key] for key in previous["findings"][0]} == previous["findings"][0]
        assert migrated["revision"] == migrated["reopen_count"] == 0
        assert all(migrated[key] is None for key in ("owner_employee_id", "due_date", "resolved_at", "resolved_result_id"))
        assert migrated["notes"] == "" and migrated["verification_after_sequence"] == 0
        assert files(settings.evidence_dir) == evidence_files
        with legacy_factory() as db:
            assert db.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0006"
            assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0
            assert db.scalar(select(func.count()).select_from(MockSourceRevision)) == 0
            assert db.execute(text("PRAGMA foreign_key_check")).all() == []
            assert any(fk["constrained_columns"] == ["owner_employee_id"] and fk["referred_table"] == "employees" for fk in inspect(db.connection()).get_foreign_keys("findings"))
    finally:
        engine.dispose()


def test_baseline_details_expose_only_supported_named_corrections(client, baseline, seeded):
    expected = {("LOY-01", "R001"): "restore_consent_evidence", ("PAY-01", "R002"): "enable_tls", ("PAY-03", "R003"): None}
    for key, finding in baseline[2].items():
        current = detail(client, finding)
        assert current["revision"] == current["reopen_count"] == 0
        assert current["status"] == "open"
        assert current["owner_employee_id"] is None and current["due_date"] is None
        assert current["resolved_at"] is None and current["resolved_result_id"] is None
        assert current["updated_at"]
        actions = current["correction_actions"]
        assert [row["action"] for row in actions] == ([expected[key]] if expected[key] else [])
        for action in actions:
            assert action["title"] and action["description"] and action["changes"]
            assert action["already_applied"] is False
    assert seeded.runtime_source_dir == Path(seeded.database_url.removeprefix("sqlite:///")).parent / "mock_sources"


def test_assignment_patch_increments_revision_returns_detail_and_can_clear_fields(client, baseline):
    finding = baseline[2][("PAY-01", "R002")]
    previous_history = detail(client, finding)["history"]
    updated = patch(client, finding, owner_employee_id="E-GM", due_date="2026-11-30", notes="Confirm TLS with payment owner.", status="in_progress")
    assert updated == detail(client, finding)
    assert (updated["owner_employee_id"], updated["due_date"], updated["notes"], updated["status"]) == (
        "E-GM", "2026-11-30", "Confirm TLS with payment owner.", "in_progress",
    )
    assert updated["revision"] == finding["revision"] + 1
    assert updated["history"] == previous_history
    # Omitting expected_revision is explicitly supported for non-UI clients.
    response = client.patch(f"/api/findings/{finding['id']}", headers=write_headers(client), json={"owner_employee_id": None, "due_date": None, "notes": "", "status": "open"})
    assert response.status_code == 200, response.text
    cleared = response.json()
    assert all(cleared[key] is None for key in ("owner_employee_id", "due_date"))
    assert cleared["notes"] == ""
    assert cleared["status"] == "open" and cleared["revision"] == updated["revision"] + 1
    events = audit(client, finding_id=finding["id"])["items"]
    assert any(row["actor"] == "admin" and row["before"].get("owner_employee_id") is None and row["after"].get("owner_employee_id") == "E-GM" for row in events)


def test_stale_revision_conflicts_leave_database_and_runtime_sources_unchanged(client, baseline, factory, seeded):
    old = baseline[2][("PAY-01", "R002")]
    current = patch(client, old, notes="A newer editor saved this.")
    before, before_files = database_snapshot(factory), files(seeded.runtime_source_dir)
    for method, suffix, payload in [
        ("patch", "", {"notes": "stale overwrite"}),
        ("post", "/corrections", {"action": "enable_tls"}),
    ]:
        response = getattr(client, method)(f"/api/findings/{old['id']}{suffix}", json={**payload, "expected_revision": old["revision"]}, headers=write_headers(client))
        assert response.status_code == 409, response.text
    assert detail(client, old) == current
    assert database_snapshot(factory) == before
    assert files(seeded.runtime_source_dir) == before_files


def test_invalid_metadata_and_manual_resolution_are_atomic(client, baseline, factory):
    finding = baseline[2][("LOY-01", "R001")]
    before = database_snapshot(factory)
    for invalid in ({"owner_employee_id": "E-MISSING"}, {"due_date": "2026-02-30"}, {"status": "resolved"}):
        response = client.patch(f"/api/findings/{finding['id']}", json={"notes": "must not leak from invalid patch", **invalid}, headers=write_headers(client))
        assert response.status_code in {400, 404, 422}, response.text
        assert database_snapshot(factory) == before


def test_mutations_require_session_csrf_and_same_origin_without_side_effects(client, baseline, factory, seeded):
    finding = baseline[2][("PAY-01", "R002")]
    before, before_files = database_snapshot(factory), files(seeded.runtime_source_dir)
    headers = write_headers(client)
    bad_headers = [
        {key: value for key, value in headers.items() if key != "X-CSRF-Token"},
        {**headers, "X-CSRF-Token": "wrong-token"},
        {**headers, "Origin": "https://foreign.example"},
        {**headers, "Sec-Fetch-Site": "cross-site"},
        {key: value for key, value in headers.items() if key != "X-Requested-With"},
    ]
    for method, suffix, payload in [("patch", "", {"notes": "blocked"}), ("post", "/corrections", {"action": "enable_tls"})]:
        for request_headers in bad_headers:
            response = getattr(client, method)(f"/api/findings/{finding['id']}{suffix}", json=payload, headers=request_headers)
            assert response.status_code == 403, response.text
    assert database_snapshot(factory) == before
    assert files(seeded.runtime_source_dir) == before_files


def test_remediation_and_audit_require_general_manager_and_login(client, baseline, factory):
    finding = baseline[2][("PAY-01", "R002")]
    headers = write_headers(client)
    with factory.begin() as db:
        db.scalar(select(User)).employee_id = "E001-01"
    for expected in (403, 401):
        assert client.get("/api/audit-events").status_code == expected
        assert client.get(f"/api/findings/{finding['id']}").status_code == expected
        assert client.patch(f"/api/findings/{finding['id']}", json={"notes": "blocked"}, headers=headers).status_code == expected
        assert client.post(f"/api/findings/{finding['id']}/corrections", json={"action": "enable_tls"}, headers=headers).status_code == expected
        client.cookies.clear()


def test_restore_consent_creates_coherent_runtime_revision_without_rewriting_history(client, baseline, seeded, factory):
    original = files(FIXTURES)
    original_evidence = files(seeded.evidence_dir)
    original_finances = financial_and_visit_records(factory)
    bundles = load_sources()
    finding = baseline[2][("LOY-01", "R001")]
    previous_history = detail(client, finding)["history"]
    result = correct(client, finding, "restore_consent_evidence")
    assert result["changed"] is True and result["revision_id"]
    current = result["finding"]
    assert current["status"] == "awaiting_verification" and current["resolved_at"] is None
    assert current["revision"] == finding["revision"] + 1
    assert current["history"] == previous_history
    directory = revision_directory(seeded, factory, result["revision_id"])
    source_files(directory)
    corrected = load_sources(directory)
    events = corrected["loyalty"]["datasets"]["consent_events"]
    added = [row for row in events if row["id"] == "CE-C001-01-FIX"]
    assert len(added) == 1
    assert added[0] == dict(id="CE-C001-01-FIX", customer_id="C001-01", purpose_id="PUR-MARKETING", event_type="grant", occurred_at="2026-09-01T10:00:00Z", notice_version="privacy-2026-09")
    assert [row for row in events if row["id"] != added[0]["id"]] == bundles["loyalty"]["datasets"]["consent_events"]
    assert instant(corrected["loyalty"]["captured_at"]) == NOW
    assert corrected["erp"]["datasets"] == bundles["erp"]["datasets"]
    assert instant(corrected["erp"]["captured_at"]) == NOW
    assert corrected["payment"] == bundles["payment"]
    for dataset, rows in bundles["loyalty"]["datasets"].items():
        if dataset != "consent_events":
            assert corrected["loyalty"]["datasets"][dataset] == rows, dataset
    for path in (directory / "visits").glob("*.json"):
        payload = json.loads(path.read_bytes())
        assert instant(payload["captured_at"]) == NOW
        assert payload["records"] == json.loads(original[f"visits/{path.name}"])["records"]
    assert files(FIXTURES) == original
    assert financial_and_visit_records(factory) == original_finances
    assert {name: files(seeded.evidence_dir)[name] for name in original_evidence} == original_evidence
    assert client.get(f"/api/assessments/{baseline[0]['id']}/results", params={"limit": 100}).json()["items"] == baseline[1]
    with factory() as db:
        assert db.get(d.ConsentEvent, added[0]["id"]) is not None
        assert db.execute(text("PRAGMA foreign_key_check")).all() == []


def test_tls_revision_builds_on_prior_correction_and_keeps_financial_data_immutable(client, baseline, seeded, factory):
    original_finances = financial_and_visit_records(factory)
    first = correct(client, baseline[2][("LOY-01", "R001")], "restore_consent_evidence")
    first_directory = revision_directory(seeded, factory, first["revision_id"])
    first_files = files(first_directory)
    first_sources = load_sources(first_directory)
    original = files(FIXTURES)
    second = correct(client, baseline[2][("PAY-01", "R002")], "enable_tls")
    assert second["changed"] is True and second["revision_id"] != first["revision_id"]
    second_directory = revision_directory(seeded, factory, second["revision_id"])
    source_files(second_directory)
    corrected = load_sources(second_directory)
    expected = deepcopy(first_sources)
    expected["payment"]["captured_at"] = corrected["payment"]["captured_at"]
    expected["payment"]["source_version"] = corrected["payment"]["source_version"]
    next(row for row in expected["payment"]["datasets"]["payment_configurations"] if row["asset_id"] == "R002-payment")["settings_json"]["tls_enabled"] = True
    assert corrected == expected
    assert instant(corrected["payment"]["captured_at"]) == NOW
    assert files(first_directory) == first_files
    assert files(FIXTURES) == original
    assert financial_and_visit_records(factory) == original_finances
    with factory() as db:
        assert db.get(d.PaymentConfiguration, "R002-payment").settings_json["tls_enabled"] is True
        assert db.get(d.ConsentEvent, "CE-C001-01-FIX") is not None


def test_reapplying_correction_creates_no_duplicate_source_snapshot_or_consent(client, baseline, seeded, factory):
    finding = baseline[2][("LOY-01", "R001")]
    applied = correct(client, finding, "restore_consent_evidence")
    before_counts, before_files = counts(factory), files(seeded.runtime_source_dir)
    before_evidence = files(seeded.evidence_dir)
    again = correct(client, applied["finding"], "restore_consent_evidence")
    assert again["changed"] is False and again["revision_id"] is None
    assert counts(factory) == before_counts
    assert files(seeded.runtime_source_dir) == before_files
    assert files(seeded.evidence_dir) == before_evidence
    assert again["finding"]["revision"] == applied["finding"]["revision"]
    assert next(row for row in again["finding"]["correction_actions"] if row["action"] == "restore_consent_evidence")["already_applied"] is True
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(d.ConsentEvent).where(d.ConsentEvent.id == "CE-C001-01-FIX")) == 1


def test_corrected_full_scan_resolves_only_confirmed_targets_and_reaches_98_percent(client, baseline):
    for key, action in [(("LOY-01", "R001"), "restore_consent_evidence"), (("PAY-01", "R002"), "enable_tls")]:
        corrected = correct(client, baseline[2][key], action)["finding"]
        assert corrected["status"] == "awaiting_verification" and corrected["resolved_result_id"] is None
    verified, results = scan(client)
    assert Counter(row["status"] for row in results) == {"pass": 49, "fail": 1}
    assert verified["summary"]["pass_rate"] == 98
    current = keyed(findings(client)["items"])
    assert {key for key, row in current.items() if row["status"] != "resolved"} == {("PAY-03", "R003")}
    for key in [("LOY-01", "R001"), ("PAY-01", "R002")]:
        finding = detail(client, current[key])
        assert finding["id"] == baseline[2][key]["id"]
        assert finding["status"] == "resolved" and finding["resolved_at"]
        assert finding["resolved_result_id"] == keyed(results)[key]["id"]
        assert finding["first_result_id"] == finding["last_result_id"] == baseline[2][key]["last_result_id"]
        assert finding["occurrences"] == 1 and len(finding["history"]) == 1
    assert client.get(f"/api/assessments/{baseline[0]['id']}/results", params={"limit": 100}).json()["items"] == baseline[1]


def test_out_of_scope_scans_preserve_assignments_and_verification_state(client, baseline):
    finding = patch(client, baseline[2][("PAY-01", "R002")], owner_employee_id="E-GM", notes="Keep until verified", due_date="2026-12-01", status="in_progress")
    waiting = correct(client, finding, "enable_tls")["finding"]
    scan(client, ["R001"], ["PCI-DSS"])
    scan(client, ["R002"], ["GDPR"])
    assert detail(client, waiting) == waiting
    _, results = scan(client, ["R002"], ["PCI-DSS"])
    resolved = detail(client, waiting)
    assert resolved["status"] == "resolved" and resolved["resolved_result_id"] == keyed(results)[("PAY-01", "R002")]["id"]
    assert all(resolved[key] == waiting[key] for key in ("owner_employee_id", "notes", "due_date"))


@pytest.mark.parametrize("uncertain", ["unknown", "error", "not_applicable"])
def test_inconclusive_or_inapplicable_evaluation_never_resolves_awaiting_finding(client, baseline, seeded, factory, uncertain):
    waiting = correct(client, baseline[2][("PAY-01", "R002")], "enable_tls")["finding"]
    with factory() as db:
        evidence = db.get(d.Evidence, db.get(d.PaymentConfiguration, "R002-payment").evidence_id)
        path = seeded.evidence_dir / evidence.relative_path
    if uncertain == "unknown":
        path.unlink()
    elif uncertain == "error":
        path.write_text("{}")
    else:
        rewrite_snapshot(seeded, factory, "assets", lambda rows: next(row for row in rows if row["id"] == "R002-payment").update(active=False))
    _, results = scan(client, ["R002"], ["PCI-DSS"])
    assert keyed(results)[("PAY-01", "R002")]["status"] == uncertain
    current = detail(client, waiting)
    assert current["status"] == "awaiting_verification"
    assert current["resolved_at"] is None and current["resolved_result_id"] is None
    assert all(current[key] == waiting[key] for key in ("revision", "last_result_id", "occurrences", "reopen_count"))


def test_failed_verification_keeps_finding_unresolved_and_appends_failure_history(client, baseline):
    finding = patch(client, baseline[2][("PAY-01", "R002")], status="awaiting_verification", notes="Verify external work")
    _, failed_results = scan(client, ["R002"], ["PCI-DSS"])
    current = detail(client, finding)
    assert keyed(failed_results)[("PAY-01", "R002")]["status"] == "fail"
    assert current["status"] in {"open", "in_progress", "awaiting_verification"}
    assert current["resolved_at"] is None and current["reopen_count"] == 0
    assert current["occurrences"] == 2


@pytest.mark.parametrize("guard", ["new_verification_request", "source_replaced"])
def test_old_queued_passing_scan_cannot_bypass_verification_or_current_source_guard(client, baseline, seeded, guard):
    from app.assessments import AssessmentInput

    # Select passing source evidence first, then queue a scan without running it.
    passing_sources = load_sources()
    next(row for row in passing_sources["payment"]["datasets"]["payment_configurations"] if row["asset_id"] == "R002-payment")["settings_json"]["tls_enabled"] = True
    import_sources(seeded, passing_sources)
    service = client.app.state.assessment_service
    queued = service.queue(AssessmentInput(restaurant_ids=["R002"], framework_ids=["PCI-DSS"]), "admin")
    if guard == "new_verification_request":
        waiting = patch(client, baseline[2][("PAY-01", "R002")], status="awaiting_verification")
    else:
        import_sources(seeded, load_sources())
        waiting = detail(client, baseline[2][("PAY-01", "R002")])
    service.run(queued["id"])
    completed = client.get(f"/api/assessments/{queued['id']}").json()
    assert completed["status"] == "completed"
    assert keyed(completed["results"])[("PAY-01", "R002")]["status"] == "pass"
    assert detail(client, waiting)["status"] == waiting["status"]
    assert detail(client, waiting)["resolved_result_id"] is None
    if guard == "source_replaced":
        import_sources(seeded, passing_sources)
    _, fresh = scan(client, ["R002"], ["PCI-DSS"])
    resolved = detail(client, waiting)
    assert resolved["status"] == "resolved"
    assert resolved["resolved_result_id"] == keyed(fresh)[("PAY-01", "R002")]["id"]


def test_shared_target_requires_all_known_affected_restaurants_to_pass(client, seeded):
    bundles = load_sources()
    next(row for row in bundles["erp"]["datasets"]["assets"] if row["id"] == "R004-payment")["restaurant_id"] = None
    shared_configuration = next(row for row in bundles["payment"]["datasets"]["payment_configurations"] if row["asset_id"] == "R004-payment")
    shared_configuration["settings_json"]["tls_enabled"] = False
    import_sources(seeded, bundles)
    scan(client, frameworks=["PCI-DSS"])
    shared = next(row for row in findings(client, control_id="PAY-01")["items"] if row["target_type"] == "shared_system")
    assert shared["affected_restaurant_ids"] == RESTAURANTS
    shared_configuration["settings_json"]["tls_enabled"] = True
    import_sources(seeded, bundles)
    scan(client, ["R001"], ["PCI-DSS"])
    assert detail(client, shared)["status"] == "open"
    _, results = scan(client, frameworks=["PCI-DSS"])
    resolved = detail(client, shared)
    assert resolved["status"] == "resolved" and resolved["occurrences"] == 1
    assert resolved["resolved_result_id"] in {row["id"] for row in results if row["control_id"] == "PAY-01"}
    assert next(row for row in findings(client, control_id="PAY-01")["items"] if row["restaurant_id"] == "R002")["status"] == "open"


def test_private_target_resolves_when_only_another_shared_target_still_fails(client, baseline, seeded):
    bundles = load_sources()
    next(row for row in bundles["erp"]["datasets"]["assets"] if row["id"] == "R004-payment")["restaurant_id"] = None
    for configuration in bundles["payment"]["datasets"]["payment_configurations"]:
        if configuration["asset_id"] in {"R002-payment", "R004-payment"}:
            configuration["settings_json"]["tls_enabled"] = configuration["asset_id"] == "R002-payment"
    import_sources(seeded, bundles)
    _, results = scan(client, frameworks=["PCI-DSS"])
    assert keyed(results)[("PAY-01", "R002")]["status"] == "fail"
    private = detail(client, baseline[2][("PAY-01", "R002")])
    assert private["status"] == "resolved" and private["latest_evaluation_status"] == "pass"
    assert private["occurrences"] == 1 and len(private["history"]) == 1
    assert private["history"][0]["assessment_id"] == baseline[0]["id"]


def test_recurring_failure_reopens_same_finding_once_and_preserves_assignment(client, baseline, seeded):
    original = patch(client, baseline[2][("PAY-01", "R002")], owner_employee_id="E-GM", notes="Watch for configuration drift", due_date="2026-12-01")
    correct(client, original, "enable_tls")
    scan(client, ["R002"], ["PCI-DSS"])
    resolved = detail(client, original)
    assert resolved["status"] == "resolved"
    import_sources(seeded, load_sources())
    recurrent, _ = scan(client, ["R002"], ["PCI-DSS"])
    reopened = detail(client, original)
    assert reopened["id"] == original["id"] and reopened["status"] == "open"
    assert reopened["reopen_count"] == 1 and reopened["occurrences"] == 2
    assert reopened["resolved_at"] is None and reopened["resolved_result_id"] is None
    assert reopened["first_result_id"] == original["first_result_id"]
    assert reopened["revision"] > resolved["revision"]
    assert all(reopened[key] == original[key] for key in ("owner_employee_id", "due_date", "notes"))
    assert {row["assessment_id"] for row in reopened["history"]} == {baseline[0]["id"], recurrent["id"]}
    scan(client, ["R002"], ["PCI-DSS"])
    assert detail(client, original)["reopen_count"] == 1
    assert findings(client, control_id="PAY-01")["total"] == 1


def test_unknown_unsupported_and_active_assessment_corrections_are_rejected_atomically(client, baseline, seeded, factory):
    from app.assessments import AssessmentInput

    for finding, action in [
        (baseline[2][("PAY-01", "R002")], "invented_action"),
        (baseline[2][("PAY-03", "R003")], "enable_tls"),
    ]:
        before, before_files = database_snapshot(factory), files(seeded.runtime_source_dir)
        response = client.post(f"/api/findings/{finding['id']}/corrections", json={"action": action}, headers=write_headers(client))
        assert response.status_code in {400, 422}, response.text
        assert database_snapshot(factory) == before and files(seeded.runtime_source_dir) == before_files
    queued = client.app.state.assessment_service.queue(AssessmentInput(restaurant_ids=["R002"], framework_ids=["PCI-DSS"]), "admin")
    before, before_files = database_snapshot(factory), files(seeded.runtime_source_dir)
    finding = baseline[2][("PAY-01", "R002")]
    response = client.post(f"/api/findings/{finding['id']}/corrections", json={"action": "enable_tls"}, headers=write_headers(client))
    assert response.status_code == 409, response.text
    assert database_snapshot(factory) == before and files(seeded.runtime_source_dir) == before_files
    assert client.get(f"/api/assessments/{queued['id']}").json()["status"] == "queued"


def test_import_failure_rolls_back_source_files_imported_rows_finding_and_audit(client, baseline, seeded, factory, monkeypatch):
    from app import remediation

    original_import = remediation.import_sources
    reached_import = []

    def fail_after_import(*args, **kwargs):
        result = original_import(*args, **kwargs)
        reached_import.append(result)
        raise ValueError("Injected connector failure after source writes")

    before = database_snapshot(factory)
    before_sources, before_evidence = files(seeded.runtime_source_dir), files(seeded.evidence_dir)
    monkeypatch.setattr(remediation, "import_sources", fail_after_import)
    # Reuse the running app without entering its startup/shutdown lifecycle again.
    request_client = TestClient(client.app, raise_server_exceptions=False)
    try:
        request_client.cookies.update(client.cookies)
        response = request_client.post(
            f"/api/findings/{baseline[2][('PAY-01', 'R002')]['id']}/corrections",
            json={"action": "enable_tls"}, headers=write_headers(request_client),
        )
        assert response.status_code in {400, 409, 422, 500}, response.text
    finally:
        request_client.close()
    assert reached_import, "The failure must occur after the connector has written inside the transaction"
    assert database_snapshot(factory) == before
    assert files(seeded.runtime_source_dir) == before_sources
    assert files(seeded.evidence_dir) == before_evidence
    monkeypatch.setattr(remediation, "import_sources", original_import)
    assert correct(client, baseline[2][("PAY-01", "R002")], "enable_tls")["changed"] is True


def test_restart_preserves_assignment_corrections_audit_and_source_revision(client, baseline, seeded, factory):
    assigned = patch(client, baseline[2][("PAY-01", "R002")], owner_employee_id="E-GM", due_date="2026-12-01", notes="Persistent remediation", status="in_progress")
    corrected = correct(client, assigned, "enable_tls")
    previous_detail = detail(client, assigned)
    previous_audit = audit(client)
    previous_files, previous_counts = files(seeded.runtime_source_dir), counts(factory)
    client.app.state.scheduler.stop()
    with TestClient(create_app(seeded)) as restarted:
        response = restarted.post("/api/auth/login", json={"username": "admin", "password": PASSWORD}, headers=HEADERS)
        assert response.status_code == 200, response.text
        assert detail(restarted, assigned) == previous_detail
        assert audit(restarted) == previous_audit
        assert files(seeded.runtime_source_dir) == previous_files
        assert counts(factory) == previous_counts
        assert correct(restarted, previous_detail, "enable_tls")["changed"] is False
        scan(restarted, ["R002"], ["PCI-DSS"])
        assert detail(restarted, assigned)["status"] == "resolved"
    assert revision_directory(seeded, factory, corrected["revision_id"]).is_dir()


def test_audit_records_lifecycle_before_after_links_filters_and_stable_pagination(client, baseline, seeded):
    finding = baseline[2][("PAY-01", "R002")]
    assigned = patch(client, finding, owner_employee_id="E-GM", status="in_progress")
    corrected = correct(client, assigned, "enable_tls")
    verification, _ = scan(client, ["R002"], ["PCI-DSS"])
    import_sources(seeded, load_sources())
    recurrence, _ = scan(client, ["R002"], ["PCI-DSS"])
    page = audit(client)
    assert page["total"] == len(page["items"]) and page["total"] >= 5
    assert len({row["id"] for row in page["items"]}) == page["total"]
    for event in page["items"]:
        assert {"id", "finding_id", "assessment_id", "actor", "event_type", "occurred_at", "before", "after", "details"} <= event.keys()
        assert event["event_type"] and event["actor"] and instant(event["occurred_at"]).tzinfo
    selected = audit(client, finding_id=finding["id"])["items"]
    assert selected and all(row["finding_id"] == finding["id"] for row in selected)
    assert any(row["before"].get("status") == "awaiting_verification" and row["after"].get("status") == "resolved" and row["assessment_id"] == verification["id"] for row in selected)
    assert any(row["before"].get("status") == "resolved" and row["after"].get("status") == "open" and row["assessment_id"] == recurrence["id"] for row in selected)
    assert any(corrected["revision_id"] in json.dumps(row) for row in selected)
    by_assessment = audit(client, assessment_id=verification["id"])["items"]
    assert by_assessment and all(row["assessment_id"] == verification["id"] for row in by_assessment)
    event_type = selected[0]["event_type"]
    filtered = audit(client, finding_id=finding["id"], event_type=event_type)
    assert {row["id"] for row in filtered["items"]} == {row["id"] for row in selected if row["event_type"] == event_type}
    first, second = audit(client, limit=2), audit(client, limit=2, offset=2)
    assert first["total"] == second["total"] == page["total"]
    assert first["items"] + second["items"] == page["items"][:4]
    assert audit(client, offset=page["total"])["items"] == []
    assert audit(client, finding_id="F-NONEXISTENT")["total"] == 0


def test_reset_removes_managed_phase_four_state_but_preserves_sessions_and_unmanaged_files(client, baseline, seeded, factory):
    from app.remediation_models import AuditEvent, MockSourceRevision

    correction = correct(client, baseline[2][("PAY-01", "R002")], "enable_tls")
    directory = revision_directory(seeded, factory, correction["revision_id"])
    managed_sources = list(source_files(directory))
    token = client.cookies.get(COOKIE_NAME)
    with factory() as db:
        user = db.scalar(select(User))
        credentials = (user.id, user.username, user.password_hash)
    seeded.runtime_source_dir.mkdir(parents=True, exist_ok=True)
    unmanaged = seeded.runtime_source_dir / "operator-notes.txt"
    unmanaged.write_text("Keep this local note.")
    evidence_note = seeded.evidence_dir / "operator-notes.txt"
    evidence_note.write_text("Keep this evidence note.")
    original = files(FIXTURES)
    reset_demo(seeded)
    with factory() as db:
        for model in (AuditEvent, MockSourceRevision, Finding, ResultEvidence, AssessmentResult, Assessment, d.Evidence, d.CollectionRun):
            assert db.scalar(select(func.count()).select_from(model)) == 0
        user = db.scalar(select(User))
        assert (user.id, user.username, user.password_hash) == credentials
        assert db.get(LoginSession, hash_token(token)) is not None
        assert db.execute(text("PRAGMA foreign_key_check")).all() == []
    assert all(not (directory / name).exists() for name in managed_sources)
    assert unmanaged.read_text() == "Keep this local note."
    assert evidence_note.read_text() == "Keep this evidence note."
    assert files(FIXTURES) == original
    assert client.get("/api/auth/me").status_code == 200
    import_sources(seeded, load_sources())
    _, results = scan(client)
    assert Counter(row["status"] for row in results) == {"pass": 47, "fail": 3}
