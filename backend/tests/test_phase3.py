"""Phase 3 integration tests against isolated imports and immutable evidence."""

from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import asyncio
import hashlib
import json

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from starlette.background import BackgroundTask, BackgroundTasks

from app import domain as d
from app.bootstrap import BACKEND_DIR, bootstrap
from app.config import Settings
from app.database import create_database_engine, session_factory
from app.main import COOKIE_NAME, create_app, hash_token
from app.models import LoginSession, User
from app.seed import import_sources, load_sources, reset_demo


PASSWORD = "phase-three-test-password"
HEADERS = {"X-Requested-With": "XMLHttpRequest", "Origin": "http://testserver"}
NOW = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
RESTAURANTS = [f"R{i:03}" for i in range(1, 6)]
FRAMEWORKS = ["GDPR", "PCI-DSS"]
BASELINE_FAILURES = {("LOY-01", "R001"), ("PAY-01", "R002"), ("PAY-03", "R003")}


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        admin_username="admin",
        admin_password=PASSWORD,
        database_url=f"sqlite:///{tmp_path / 'phase3.db'}",
        evidence_dir=tmp_path / "evidence",
        app_origin="http://testserver",
        seed_demo_data=False,
    )


@pytest.fixture
def seeded(settings, monkeypatch):
    from app import assessments

    monkeypatch.setattr(assessments, "utcnow", lambda: NOW)
    bootstrap(settings)
    import_sources(settings, load_sources())
    return settings


@pytest.fixture
def client(seeded):
    with TestClient(create_app(seeded)) as client:
        response = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": PASSWORD},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.text
        yield client


@pytest.fixture
def factory(seeded):
    engine = create_database_engine(seeded.database_url)
    try:
        yield session_factory(engine)
    finally:
        engine.dispose()


def write_headers(client):
    identity = client.get("/api/auth/me")
    assert identity.status_code == 200, identity.text
    return {**HEADERS, "X-CSRF-Token": identity.json()["csrf_token"]}


def scan(client, restaurants=None, frameworks=None):
    response = client.post(
        "/api/assessments",
        json={
            "restaurant_ids": RESTAURANTS if restaurants is None else restaurants,
            "framework_ids": FRAMEWORKS if frameworks is None else frameworks,
        },
        headers=write_headers(client),
    )
    assert response.status_code == 202, response.text
    accepted = response.json()
    response = client.get(f"/api/assessments/{accepted['id']}")
    assert response.status_code == 200, response.text
    assessment = response.json()
    assert assessment["status"] == "completed", assessment
    response = client.get(f"/api/assessments/{accepted['id']}/results", params={"limit": 100})
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["total"] == len(page["items"])
    assert assessment["results"] == page["items"]
    assert_summary(assessment, page["items"], len(RESTAURANTS if restaurants is None else restaurants))
    return assessment, page["items"]


def keyed(results):
    return {(row["control_id"], row["restaurant_id"]): row for row in results}


def assert_summary(assessment, results, scope_size=5):
    expected = {status: 0 for status in ["pass", "fail", "unknown", "not_applicable", "error"]}
    expected.update(Counter(row["status"] for row in results))
    summary = assessment["summary"]
    applicable = len(results) - expected["not_applicable"]
    assert summary["counts"] == expected
    assert summary["total_evaluations"] == len(results)
    assert summary["applicable_evaluations"] == applicable
    assert summary["pass_rate"] == (round(expected["pass"] * 100 / applicable, 2) if applicable else None)
    assert summary["coverage"] == {"total_restaurants": 5, "in_scope_restaurants": scope_size, "assessed_restaurants": scope_size}


def findings(client, **params):
    response = client.get("/api/findings", params={"limit": 100, **params})
    assert response.status_code == 200, response.text
    return response.json()


def rewrite_snapshot(settings, factory, dataset, mutate):
    """Change isolated collected evidence and its digest, bypassing the importer."""
    with factory.begin() as db:
        evidence = db.scalar(select(d.Evidence).where(d.Evidence.dataset_type == dataset))
        assert evidence is not None
        path = settings.evidence_dir / evidence.relative_path
        payload = json.loads(path.read_bytes())
        mutate(payload["records"])
        contents = json.dumps(payload).encode()
        path.write_bytes(contents)
        evidence.sha256 = hashlib.sha256(contents).hexdigest()
        evidence.record_count = len(payload["records"])


def test_phase_two_database_upgrade_preserves_imports_admin_and_session(settings):
    from app.assessment_models import Assessment, AssessmentResult, Finding, ResultEvidence

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.attributes["database_url"] = settings.database_url
    command.upgrade(config, "0003")
    engine = create_database_engine(settings.database_url)
    factory = session_factory(engine)
    try:
        with factory.begin() as db:
            db.add(User(id=1, username="existing-admin", password_hash="preserved-password-hash"))
            db.flush()
            db.add(LoginSession(token_hash="existing-session", user_id=1, csrf_token="existing-csrf", expires_at=9999999999))
        import_sources(settings, load_sources())
        original_files = {path: path.read_bytes() for path in settings.evidence_dir.rglob("*.json")}
        bootstrap(settings)
        with factory() as db:
            user = db.get(User, 1)
            assert (user.username, user.password_hash, user.employee_id) == ("existing-admin", "preserved-password-hash", "E-GM")
            assert db.get(LoginSession, "existing-session").csrf_token == "existing-csrf"
            assert db.scalar(select(func.count()).select_from(d.Restaurant)) == 5
            assert db.scalar(select(func.count()).select_from(d.Evidence)) == 22
            for model in [Assessment, AssessmentResult, Finding, ResultEvidence]:
                assert db.scalar(select(func.count()).select_from(model)) == 0
            assert db.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0006"
            assert db.execute(text("PRAGMA foreign_key_check")).all() == []
        assert original_files == {path: path.read_bytes() for path in settings.evidence_dir.rglob("*.json")}
    finally:
        engine.dispose()


def test_full_scan_has_fifty_results_and_three_expected_failures(client, factory):
    assessment, results = scan(client)
    assert len(results) == len(keyed(results)) == 50
    assert {row["restaurant_id"] for row in results} == set(RESTAURANTS)
    assert Counter(row["status"] for row in results) == {"pass": 47, "fail": 3}
    assert_summary(assessment, results)
    assert assessment["summary"]["pass_rate"] == 94
    assert {key for key, row in keyed(results).items() if row["status"] == "fail"} == BASELINE_FAILURES
    assert all(row["assessment_id"] == assessment["id"] for row in results)
    assert all(row["reason"] and row["rule_version"] for row in results)
    # The baseline includes denied writes and a formerly authorized write.
    # Neither may turn into a successful unauthorized-action finding.
    assert all(keyed(results)[("LOY-05", rid)]["status"] == "pass" for rid in RESTAURANTS)
    assert all(keyed(results)[("LOY-01", rid)]["status"] == "pass" for rid in RESTAURANTS[1:])
    page = findings(client)
    assert page["total"] == 3
    assert set(keyed(page["items"])) == BASELINE_FAILURES
    assert {row["status"] for row in page["items"]} == {"open"}
    with factory() as db:
        assert db.execute(text("PRAGMA foreign_key_check")).all() == []


def test_repeated_scan_keeps_stable_findings_and_appends_result_history(client, factory):
    from app.assessment_models import Assessment, AssessmentResult, Finding, ResultEvidence

    first, first_results = scan(client)
    original = {key: row["id"] for key, row in keyed(findings(client)["items"]).items()}
    second, second_results = scan(client)
    assert first["id"] != second["id"]
    assert {row["id"] for row in first_results}.isdisjoint(row["id"] for row in second_results)
    assert original == {key: row["id"] for key, row in keyed(findings(client)["items"]).items()}
    history = client.get("/api/assessments", params={"limit": 1}).json()
    assert history["total"] == 2 and len(history["items"]) == 1
    next_page = client.get("/api/assessments", params={"limit": 1, "offset": 1}).json()
    assert {history["items"][0]["id"], next_page["items"][0]["id"]} == {first["id"], second["id"]}
    assert client.get(f"/api/assessments/{first['id']}/results", params={"limit": 100}).json()["items"] == first_results
    with factory() as db:
        for model, count in [(Assessment, 2), (AssessmentResult, 100), (Finding, 3)]:
            assert db.scalar(select(func.count()).select_from(model)) == count
        assert db.scalar(select(func.count()).select_from(ResultEvidence)) >= 100


@pytest.mark.parametrize(
    "restaurants,frameworks,expected_count,expected_failures",
    [
        (["R004"], FRAMEWORKS, 10, set()),
        (["R001", "R002"], ["GDPR"], 10, {("LOY-01", "R001")}),
        (["R002", "R003"], ["PCI-DSS"], 10, {("PAY-01", "R002"), ("PAY-03", "R003")}),
        (RESTAURANTS, ["GDPR"], 25, {("LOY-01", "R001")}),
        (RESTAURANTS, ["PCI-DSS"], 25, {("PAY-01", "R002"), ("PAY-03", "R003")}),
    ],
)
def test_scan_respects_restaurant_and_framework_scope(client, restaurants, frameworks, expected_count, expected_failures):
    assessment, results = scan(client, restaurants, frameworks)
    assert_summary(assessment, results, len(restaurants))
    assert len(results) == expected_count
    assert {row["restaurant_id"] for row in results} == set(restaurants)
    prefixes = {"LOY" if framework == "GDPR" else "PAY" for framework in frameworks}
    assert {row["control_id"].split("-")[0] for row in results} == prefixes
    assert {key for key, row in keyed(results).items() if row["status"] == "fail"} == expected_failures
    assert findings(client)["total"] == len(expected_failures)


def test_explicitly_inapplicable_inventory_is_excluded_from_rate(client, seeded):
    bundles = load_sources()
    for asset in bundles["erp"]["datasets"]["assets"]:
        if asset["restaurant_id"] == "R004":
            asset["active"] = False
    import_sources(seeded, bundles)
    assessment, results = scan(client, ["R004"], ["GDPR"])
    assert Counter(row["status"] for row in results) == {"not_applicable": 5}
    assert assessment["summary"]["pass_rate"] is None
    assessment, results = scan(client, ["R001", "R004"], ["GDPR"])
    assert Counter(row["status"] for row in results) == {"pass": 4, "fail": 1, "not_applicable": 5}
    assert assessment["summary"]["pass_rate"] == 80
    assert findings(client)["total"] == 1


def test_results_and_findings_filters_pagination_and_details(client):
    assessment, results = scan(client)
    path = f"/api/assessments/{assessment['id']}/results"
    default_page = client.get(path).json()
    assert default_page["limit"] == default_page["total"] == len(default_page["items"]) == 50
    for params, count in [
        ({"restaurant_id": "R002"}, 10),
        ({"control_id": "PAY-01"}, 5),
        ({"status": "fail"}, 3),
        ({"restaurant_id": "R002", "control_id": "PAY-01", "status": "fail"}, 1),
    ]:
        response = client.get(path, params={"limit": 100, **params})
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["total"] == len(page["items"]) == count
        assert all(all(row[key] == value for key, value in params.items()) for row in page["items"])
    first_page = client.get(path, params={"limit": 7, "offset": 0}).json()
    second_page = client.get(path, params={"limit": 7, "offset": 7}).json()
    assert first_page["total"] == second_page["total"] == 50
    assert len(first_page["items"]) == len(second_page["items"]) == 7
    assert {row["id"] for row in first_page["items"]}.isdisjoint(row["id"] for row in second_page["items"])
    assert client.get(path, params={"offset": 50}).json()["items"] == []
    detail = client.get(f"/api/results/{results[0]['id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["id"] == results[0]["id"]
    assert detail.json()["assessment_id"] == assessment["id"]
    for params, count in [({"restaurant_id": "R001"}, 1), ({"severity": "high"}, 3), ({"control_id": "PAY-03"}, 1), ({"severity": "medium"}, 0)]:
        page = findings(client, **params)
        assert page["total"] == len(page["items"]) == count
        assert all(all(row[key] == value for key, value in params.items()) for row in page["items"])
    page = findings(client, limit=1, offset=1)
    assert page["total"] == 3 and len(page["items"]) == 1
    selected = page["items"][0]
    detail = client.get(f"/api/findings/{selected['id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["id"] == selected["id"]
    assert detail.json()["control_id"] == selected["control_id"]


@pytest.mark.parametrize("path", ["/api/assessments/missing", "/api/assessments/missing/results", "/api/results/missing", "/api/findings/missing"])
def test_missing_assessment_result_and_finding_return_404(client, path):
    assert client.get(path).status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"restaurant_ids": [], "framework_ids": FRAMEWORKS},
        {"restaurant_ids": RESTAURANTS, "framework_ids": []},
        {"restaurant_ids": ["R001", "R001"], "framework_ids": FRAMEWORKS},
        {"restaurant_ids": RESTAURANTS, "framework_ids": ["GDPR", "GDPR"]},
        {"restaurant_ids": ["R999"], "framework_ids": FRAMEWORKS},
        {"restaurant_ids": RESTAURANTS, "framework_ids": ["unknown"]},
        {"restaurant_ids": ["R001", "R999"], "framework_ids": FRAMEWORKS},
        {"restaurant_ids": RESTAURANTS, "framework_ids": ["GDPR", "unknown"]},
        {"restaurant_ids": "R001", "framework_ids": FRAMEWORKS},
        {"restaurant_ids": RESTAURANTS, "framework_ids": "GDPR"},
        {"restaurant_ids": None, "framework_ids": FRAMEWORKS},
        {"restaurant_ids": RESTAURANTS, "framework_ids": None},
    ],
)
def test_invalid_scan_selection_is_rejected_without_partial_work(client, factory, payload):
    from app.assessment_models import Assessment, AssessmentResult, Finding

    response = client.post("/api/assessments", json=payload, headers=write_headers(client))
    assert response.status_code in {400, 404, 422}, response.text
    with factory() as db:
        for model in [Assessment, AssessmentResult, Finding]:
            assert db.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("change", ["no_headers", "no_csrf", "bad_csrf", "bad_origin", "cross_site", "no_requested_with"])
def test_scan_requires_same_origin_and_session_csrf(client, factory, change):
    from app.assessment_models import Assessment

    headers = write_headers(client)
    if change == "no_headers":
        headers = {}
    elif change == "no_csrf":
        headers.pop("X-CSRF-Token")
    elif change == "bad_csrf":
        headers["X-CSRF-Token"] = "not-the-session-token"
    elif change == "bad_origin":
        headers["Origin"] = "https://other-site.example"
    elif change == "cross_site":
        headers["Sec-Fetch-Site"] = "cross-site"
    else:
        headers.pop("X-Requested-With")
    response = client.post("/api/assessments", json={"restaurant_ids": RESTAURANTS, "framework_ids": FRAMEWORKS}, headers=headers)
    assert response.status_code == 403, response.text
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Assessment)) == 0


def test_assessment_routes_require_login_and_general_manager(client, factory):
    assessment, results = scan(client)
    finding = findings(client)["items"][0]
    urls = [
        "/api/assessments", f"/api/assessments/{assessment['id']}",
        f"/api/assessments/{assessment['id']}/results", f"/api/results/{results[0]['id']}",
        "/api/findings", f"/api/findings/{finding['id']}",
    ]
    headers = write_headers(client)
    with factory.begin() as db:
        db.scalar(select(User)).employee_id = "E001-01"
    for url in urls:
        assert client.get(url).status_code == 403
    assert client.post("/api/assessments", json={"restaurant_ids": RESTAURANTS, "framework_ids": FRAMEWORKS}, headers=headers).status_code == 403
    client.cookies.clear()
    for url in urls:
        assert client.get(url).status_code == 401
    assert client.post("/api/assessments", json={"restaurant_ids": RESTAURANTS, "framework_ids": FRAMEWORKS}, headers=headers).status_code == 401


def test_unauthorized_action_is_appended_and_evaluated_at_event_time(client, seeded, factory):
    from app.assessment_models import ResultEvidence

    baseline, baseline_results = scan(client)
    assert keyed(baseline_results)[("LOY-05", "R001")]["status"] == "pass"
    imported = import_sources(seeded, load_sources(scenario="unauthorized-modification"))
    assert imported["imported_sources"] == ["loyalty"]
    _, results = scan(client)
    assert Counter(row["status"] for row in results) == {"pass": 46, "fail": 4}
    result = keyed(results)[("LOY-05", "R001")]
    assert result["status"] == "fail"
    detail = client.get(f"/api/results/{result['id']}").json()
    subjects = {row["subject_id"]: row for row in detail["details"]}
    assert subjects["EV-UNAUTHORIZED-001"]["status"] == "fail"
    assert subjects["EV-R001-denied"]["status"] == "pass"
    assert subjects["EV-R001-history"]["status"] == "pass"
    with factory() as db:
        snapshots = list(db.scalars(select(ResultEvidence).where(ResultEvidence.result_id == result["id"])))
        assert {"employee_access_events", "erp_access_approvals"} <= {row.snapshot["dataset_type"] for row in snapshots}
    assert findings(client)["total"] == 4
    historical = client.get(f"/api/assessments/{baseline['id']}/results", params={"limit": 100}).json()["items"]
    assert keyed(historical)[("LOY-05", "R001")]["status"] == "pass"


def test_remediated_import_improves_results_preserving_finding_history(client, seeded):
    original, original_results = scan(client)
    original_findings = {key: row["id"] for key, row in keyed(findings(client)["items"]).items()}
    old_files = {path: path.read_bytes() for path in seeded.evidence_dir.rglob("*.json")}
    import_sources(seeded, load_sources(scenario="remediated"))
    _, results = scan(client)
    assert Counter(row["status"] for row in results) == {"pass": 49, "fail": 1}
    assert {key for key, row in keyed(results).items() if row["status"] == "fail"} == {("PAY-03", "R003")}
    # Phase 4 resolves passing targets and retains the original findings/history.
    current = findings(client)
    assert current["total"] == 3
    assert {key: row["status"] for key, row in keyed(current["items"]).items()} == {("LOY-01", "R001"): "resolved", ("PAY-01", "R002"): "resolved", ("PAY-03", "R003"): "open"}
    assert original_findings == {key: row["id"] for key, row in keyed(current["items"]).items()}
    assert all(path.read_bytes() == content for path, content in old_files.items())
    assert client.get(f"/api/assessments/{original['id']}/results", params={"limit": 100}).json()["items"] == original_results


def test_scans_consume_imported_evidence_without_reimporting_fixtures(client, seeded, factory, monkeypatch):
    from app import seed
    from app.assessment_models import Assessment

    import_sources(seeded, load_sources(scenario="remediated"))
    original_files = {path: path.read_bytes() for path in seeded.evidence_dir.rglob("*.json")}
    with factory() as db:
        run_ids = set(db.scalars(select(d.CollectionRun.id)))
        evidence_ids = set(db.scalars(select(d.Evidence.id)))

    def unexpected_source_access(*args, **kwargs):
        pytest.fail("Assessment must consume imported evidence without loading or reimporting source fixtures")

    monkeypatch.setattr(seed, "load_sources", unexpected_source_access)
    monkeypatch.setattr(seed, "import_sources", unexpected_source_access)
    assessment, results = scan(client)
    assert Counter(row["status"] for row in results) == {"pass": 49, "fail": 1}
    assert original_files == {path: path.read_bytes() for path in seeded.evidence_dir.rglob("*.json")}
    with factory() as db:
        assert set(db.scalars(select(d.CollectionRun.id))) == run_ids
        assert set(db.scalars(select(d.Evidence.id))) == evidence_ids
        stored = db.get(Assessment, assessment["id"])
        assert stored.policy and stored.controls_snapshot and stored.evidence_manifest
        assert stored.policy["max_evidence_age_days"] == 30
        assert len(stored.controls_snapshot) == 10
        assert stored.total_evaluations == stored.completed_evaluations == 50
        assert stored.active_slot is None and stored.error is None
        assert stored.started_at and stored.finished_at


def test_assessment_pins_policy_controls_and_evidence_across_later_imports(client, seeded, factory):
    from app.assessment_models import Assessment, AssessmentResult, ResultEvidence

    assessment, results = scan(client)
    with factory() as db:
        stored = db.get(Assessment, assessment["id"])
        pinned = deepcopy((stored.policy, stored.controls_snapshot, stored.evidence_manifest, stored.summary))
        references = list(db.scalars(select(ResultEvidence).join(AssessmentResult).where(AssessmentResult.assessment_id == assessment["id"])))
        assert references
        old_links = {(ref.result_id, ref.evidence_id): deepcopy(ref.snapshot) for ref in references}
        assert {ref.result_id for ref in references} == {row["id"] for row in results}
        for ref in references:
            snapshot = json.dumps(ref.snapshot)
            evidence = db.get(d.Evidence, ref.evidence_id)
            assert evidence.id in snapshot and evidence.sha256 in snapshot
            assert evidence.id in json.dumps(stored.evidence_manifest)
    result_id = keyed(results)[("LOY-01", "R001")]["id"]
    original_detail = client.get(f"/api/results/{result_id}").json()
    import_sources(seeded, load_sources(scenario="remediated"))
    scan(client)
    with factory() as db:
        stored = db.get(Assessment, assessment["id"])
        assert (stored.policy, stored.controls_snapshot, stored.evidence_manifest, stored.summary) == pinned
        references = db.scalars(select(ResultEvidence).join(AssessmentResult).where(AssessmentResult.assessment_id == assessment["id"]))
        assert {(ref.result_id, ref.evidence_id): ref.snapshot for ref in references} == old_links
    assert client.get(f"/api/results/{result_id}").json() == original_detail


def test_queued_scan_pins_imported_evidence_and_rejects_overlap(client, seeded, monkeypatch):
    pending = []

    def defer_task(self, function, *args, **kwargs):
        pending.append(BackgroundTask(function, *args, **kwargs))

    # Defer the actual worker, preserving its exact callable and arguments, so
    # a newer import can arrive between submission and evaluation.
    monkeypatch.setattr(BackgroundTasks, "add_task", defer_task)
    payload = {"restaurant_ids": RESTAURANTS, "framework_ids": FRAMEWORKS}
    headers = write_headers(client)
    accepted = client.post("/api/assessments", json=payload, headers=headers)
    assert accepted.status_code == 202, accepted.text
    assessment_id = accepted.json()["id"]
    assert client.get(f"/api/assessments/{assessment_id}").json()["status"] == "queued"
    assert len(pending) == 1
    overlap = client.post("/api/assessments", json=payload, headers=headers)
    assert overlap.status_code == 409, overlap.text
    assert len(pending) == 1
    with pytest.raises(ValueError, match="assessment"):
        reset_demo(seeded)
    assert client.get("/api/restaurants").json()["total"] == 5
    import_sources(seeded, load_sources(scenario="remediated"))
    asyncio.run(pending.pop()())
    completed = client.get(f"/api/assessments/{assessment_id}").json()
    assert completed["status"] == "completed", completed
    results = client.get(f"/api/assessments/{assessment_id}/results", params={"limit": 100}).json()
    assert results["total"] == 50
    assert Counter(row["status"] for row in results["items"]) == {"pass": 47, "fail": 3}
    # Completion releases the slot for a scan of the newly imported snapshots.
    accepted = client.post("/api/assessments", json=payload, headers=headers)
    assert accepted.status_code == 202, accepted.text
    assert len(pending) == 1
    asyncio.run(pending.pop()())
    results = client.get(f"/api/assessments/{accepted.json()['id']}/results", params={"limit": 100}).json()
    assert Counter(row["status"] for row in results["items"]) == {"pass": 49, "fail": 1}


@pytest.mark.parametrize("key", ["tls_enabled", "tls_version"])
def test_missing_required_configuration_is_unknown(client, seeded, factory, key):
    # The importer rejects incomplete rows. Simulate already-collected incomplete
    # evidence with a matching digest so the evaluator must validate its fields.
    def remove_field(rows):
        configuration = next(row for row in rows if row["asset_id"] == "R004-payment")
        configuration["settings_json"].pop(key)

    rewrite_snapshot(seeded, factory, "payment_configurations", remove_field)
    _, results = scan(client)
    assert keyed(results)[("PAY-01", "R004")]["status"] == "unknown"
    assert all(row["status"] == "unknown" and row["reason"] for row in results if row["control_id"].startswith("PAY-"))
    assert Counter(row["status"] for row in results) == {"pass": 24, "fail": 1, "unknown": 25}
    assert set(keyed(findings(client)["items"])) == {("LOY-01", "R001")}


def test_withdrawal_requires_valid_reconsent_before_later_activity(client, seeded, factory):
    def move_activity_after_withdrawal(rows):
        next(row for row in rows if row["id"] == "ACT-C004-10")["occurred_at"] = "2026-10-20T10:00:00Z"

    rewrite_snapshot(seeded, factory, "customer_activities", move_activity_after_withdrawal)
    first, results = scan(client, ["R004"], ["GDPR"])
    assert keyed(results)[("LOY-02", "R004")]["status"] == "fail"
    subjects = {row["subject_id"]: row for row in keyed(results)[("LOY-02", "R004")]["details"]}
    assert subjects["ACT-C004-10"]["status"] == "fail"

    def append_incomplete_reconsent(rows):
        rows.append({"id": "CE-R004-RECONSENT", "customer_id": "C004-10", "purpose_id": "PUR-MARKETING", "event_type": "grant", "occurred_at": "2026-10-18T10:00:00Z", "notice_version": None})

    rewrite_snapshot(seeded, factory, "consent_events", append_incomplete_reconsent)
    _, results = scan(client, ["R004"], ["GDPR"])
    assert keyed(results)[("LOY-02", "R004")]["status"] == "fail"
    assert keyed(results)[("LOY-01", "R004")]["status"] == "fail"

    def supply_notice(rows):
        next(row for row in rows if row["id"] == "CE-R004-RECONSENT")["notice_version"] = "privacy-2026-10"

    rewrite_snapshot(seeded, factory, "consent_events", supply_notice)
    _, results = scan(client, ["R004"], ["GDPR"])
    assert Counter(row["status"] for row in results) == {"pass": 5}
    old = client.get(f"/api/assessments/{first['id']}/results").json()["items"]
    assert keyed(old)[("LOY-02", "R004")]["status"] == "fail"


@pytest.mark.parametrize(
    "control_id,dataset,subject_id,changes",
    [
        ("LOY-03", "loyalty_records", "LR-C004-01", {"retention_due_at": "2026-11-16T12:00:00Z"}),
        ("LOY-04", "customer_requests", "REQ-R004", {"status": "open", "completed_at": None}),
    ],
)
def test_retention_and_overdue_request_failures(client, seeded, factory, control_id, dataset, subject_id, changes):
    def change_subject(rows):
        next(row for row in rows if row["id"] == subject_id).update(changes)

    rewrite_snapshot(seeded, factory, dataset, change_subject)
    _, results = scan(client, ["R004"], ["GDPR"])
    assert Counter(row["status"] for row in results) == {"pass": 4, "fail": 1}
    result = keyed(results)[(control_id, "R004")]
    assert result["status"] == "fail"
    assert any(row["subject_id"] == subject_id and row["status"] == "fail" for row in result["details"])
    assert set(keyed(findings(client)["items"])) == {(control_id, "R004")}


@pytest.mark.parametrize(
    "control_id,field,value",
    [
        ("PAY-02", "mfa_enabled", False),
        ("PAY-02", "password_length", 8),
        ("PAY-04", "installed_patch_version", "2026.01"),
        ("PAY-05", "prohibited_storage_detected", True),
    ],
)
def test_payment_authentication_patch_and_storage_failures(client, seeded, factory, control_id, field, value):
    def change_setting(rows):
        settings = next(row for row in rows if row["asset_id"] == "R004-payment")["settings_json"]
        target = settings["accounts"][0] if control_id == "PAY-02" else settings
        target[field] = value

    rewrite_snapshot(seeded, factory, "payment_configurations", change_setting)
    _, results = scan(client, ["R004"], ["PCI-DSS"])
    assert Counter(row["status"] for row in results) == {"pass": 4, "fail": 1}
    result = keyed(results)[(control_id, "R004")]
    assert result["status"] == "fail" and result["reason"]
    assert any(row["status"] == "fail" for row in result["details"])
    assert set(keyed(findings(client)["items"])) == {(control_id, "R004")}


def test_incomplete_source_metadata_is_unknown_instead_of_passing(client, seeded):
    bundles = load_sources()
    bundles["payment"]["complete"] = False
    import_sources(seeded, bundles)
    _, results = scan(client)
    payment = [row for row in results if row["control_id"].startswith("PAY-")]
    assert len(payment) == 25
    assert {row["status"] for row in payment} == {"unknown"}
    assert Counter(row["status"] for row in results) == {"pass": 24, "fail": 1, "unknown": 25}
    assert set(keyed(findings(client)["items"])) == {("LOY-01", "R001")}


def test_evidence_older_than_thirty_day_policy_is_unknown(client, monkeypatch):
    from app import assessments

    monkeypatch.setattr(assessments, "utcnow", lambda: NOW + timedelta(days=31))
    _, results = scan(client)
    assert Counter(row["status"] for row in results) == {"unknown": 50}
    assert all(row["reason"] for row in results)
    assert findings(client)["total"] == 0


def test_missing_restaurant_evidence_scope_is_unknown_despite_existing_imported_rows(client, factory):
    first, first_results = scan(client)
    with factory.begin() as db:
        evidence = db.scalar(select(d.Evidence).where(d.Evidence.dataset_type == "payment_configurations"))
        evidence.restaurant_ids = [rid for rid in RESTAURANTS if rid != "R001"]
        assert db.get(d.PaymentConfiguration, "R001-payment") is not None
    _, results = scan(client, ["R001"], ["PCI-DSS"])
    assert Counter(row["status"] for row in results) == {"unknown": 5}
    with factory() as db:
        assert db.execute(text("PRAGMA foreign_key_check")).all() == []
    assert client.get(f"/api/assessments/{first['id']}/results", params={"limit": 100}).json()["items"] == first_results
    assert findings(client)["total"] == 3


def test_worker_failure_preserves_partial_results_and_retry_creates_new_history(client, factory, monkeypatch):
    from app import assessments
    from app.assessment_models import Assessment, AssessmentResult, ResultEvidence

    evaluate = assessments.evaluate_control
    calls = 0

    def interrupted(control_id, restaurant_id, context):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise RuntimeError("Simulated worker interruption")
        return evaluate(control_id, restaurant_id, context)

    monkeypatch.setattr(assessments, "evaluate_control", interrupted)
    response = client.post("/api/assessments", json={"restaurant_ids": RESTAURANTS, "framework_ids": FRAMEWORKS}, headers=write_headers(client))
    assert response.status_code == 202, response.text
    assessment_id = response.json()["id"]
    failed = client.get(f"/api/assessments/{assessment_id}").json()
    assert failed["status"] == "failed" and failed["error"]
    assert failed["completed_evaluations"] == len(failed["results"]) == 3
    assert failed["summary"]["pass_rate"] is None
    assert failed["summary"]["coverage"]["assessed_restaurants"] == 0
    assert findings(client)["total"] == 0
    with factory() as db:
        assert db.get(Assessment, assessment_id).active_slot is None
        assert db.scalar(select(func.count()).select_from(ResultEvidence)) > 0
    monkeypatch.setattr(assessments, "evaluate_control", evaluate)
    retry = client.post(f"/api/assessments/{assessment_id}/retry", headers=write_headers(client))
    assert retry.status_code == 202, retry.text
    assert retry.json()["id"] != assessment_id
    completed = client.get(f"/api/assessments/{retry.json()['id']}").json()
    assert completed["status"] == "completed"
    assert Counter(row["status"] for row in completed["results"]) == {"pass": 47, "fail": 3}
    assert_summary(completed, completed["results"])
    assert client.get(f"/api/assessments/{assessment_id}").json() == failed
    assert findings(client)["total"] == 3
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(AssessmentResult)) == 53
        assert db.execute(text("PRAGMA foreign_key_check")).all() == []


@pytest.mark.parametrize("interrupted_status", ["queued", "running"])
def test_backend_restart_recovers_interrupted_scan_and_releases_slot(client, seeded, factory, interrupted_status):
    from app.assessments import AssessmentInput
    from app.assessment_models import Assessment

    completed, _ = scan(client)
    queued = client.app.state.assessment_service.queue(AssessmentInput(restaurant_ids=["R001"], framework_ids=["GDPR"]), "admin")
    if interrupted_status == "running":
        with factory.begin() as db:
            item = db.get(Assessment, queued["id"])
            item.status = "running"
            item.started_at = NOW.isoformat()
    with pytest.raises(ValueError, match="assessment"):
        reset_demo(seeded)
    client.app.state.scheduler.stop()  # Release the old process owner before restart.
    with TestClient(create_app(seeded)) as restarted:
        restarted.cookies.update(client.cookies)
        response = restarted.get(f"/api/assessments/{queued['id']}")
        assert response.status_code == 200, response.text
        failed = response.json()
        assert failed["status"] == "failed" and failed["error"] and failed["finished_at"]
        assert failed["completed_evaluations"] == 0 and failed["results"] == []
        assert restarted.get(f"/api/assessments/{completed['id']}").json() == completed
        recovered, results = scan(restarted, ["R001"], ["GDPR"])
        assert recovered["id"] != queued["id"] and len(results) == 5
        # Calling recovery again cannot change a completed assessment.
        restarted.app.state.assessment_service.recover_interrupted()
        assert restarted.get(f"/api/assessments/{recovered['id']}").json() == recovered
    with factory() as db:
        assert db.get(Assessment, queued["id"]).active_slot is None
        assert db.execute(text("PRAGMA foreign_key_check")).all() == []


def test_tampered_snapshot_creates_error_results_and_preserves_prior_history(client, seeded, factory):
    from app.assessment_models import Assessment, AssessmentResult, ResultEvidence

    first, first_results = scan(client)
    prior_findings = {row["id"] for row in findings(client)["items"]}
    with factory() as db:
        evidence = db.scalar(select(d.Evidence).where(d.Evidence.dataset_type == "payment_configurations"))
        evidence_id = evidence.id
        path = seeded.evidence_dir / evidence.relative_path
        original_bytes = path.read_bytes()
    path.write_text("{}")
    try:
        second, results = scan(client)
        assert Counter(row["status"] for row in results) == {"pass": 24, "fail": 1, "error": 25}
        errors = [row for row in results if row["status"] == "error"]
        assert all(row["control_id"].startswith("PAY-") and row["reason"] for row in errors)
        assert {row["id"] for row in findings(client)["items"]} == prior_findings
        assert path.read_text() == "{}"
        assert client.get(f"/api/evidence/{evidence_id}/content").status_code == 409
        with factory() as db:
            assert db.get(Assessment, first["id"]).status == "completed"
            assert db.get(Assessment, second["id"]).status == "completed"
            assert db.scalar(select(func.count()).select_from(AssessmentResult)) == 100
            linked_results = set(db.scalars(select(ResultEvidence.result_id).where(ResultEvidence.evidence_id == evidence_id)))
            assert {row["id"] for row in errors} <= linked_results
        assert client.get(f"/api/assessments/{first['id']}/results", params={"limit": 100}).json()["items"] == first_results
    finally:
        path.write_bytes(original_bytes)


def test_reset_clears_phase_three_data_and_preserves_admin_and_session(client, seeded, factory):
    from app.assessment_models import Assessment, AssessmentResult, Finding, ResultEvidence

    scan(client)
    token = client.cookies.get(COOKIE_NAME)
    with factory() as db:
        user = db.scalar(select(User))
        identity = (user.id, user.username, user.password_hash)
    reset_demo(seeded)
    with factory() as db:
        for model in [Assessment, AssessmentResult, ResultEvidence, Finding, d.Evidence, d.CollectionRun]:
            assert db.scalar(select(func.count()).select_from(model)) == 0
        user = db.scalar(select(User))
        assert (user.id, user.username, user.password_hash) == identity
        assert db.get(LoginSession, hash_token(token)) is not None
        assert db.execute(text("PRAGMA foreign_key_check")).all() == []
    assert client.get("/api/auth/me").status_code == 200
    assert not list(seeded.evidence_dir.rglob("*.json"))
    import_sources(seeded, load_sources())
    _, results = scan(client)
    assert Counter(row["status"] for row in results) == {"pass": 47, "fail": 3}
