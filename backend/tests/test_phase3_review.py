"""Regressions from the Phase 3 review, using isolated parent-test fixtures."""

from copy import deepcopy

from app import domain as d
from app.assessment_models import Assessment
from app.seed import import_sources, load_sources

# Pytest discovers these imported fixtures, including their fixture dependencies.
from test_phase3 import (
    RESTAURANTS,
    client,
    factory,
    findings,
    keyed,
    rewrite_snapshot,
    scan,
    seeded,
    settings,
)


def test_reimport_original_payment_snapshot_replaces_later_tls_fix(client, seeded, factory):
    original = load_sources()
    before, before_results = scan(client, ["R002"], ["PCI-DSS"])
    original_result = keyed(before_results)[("PAY-01", "R002")]
    assert original_result["status"] == "fail"
    original_evidence_id = next(
        row["id"] for row in original_result["evidence"]
        if row["dataset_type"] == "payment_configurations"
    )

    # Change only mutable payment settings, preserving all immutable consent events.
    corrected = deepcopy(original)
    payment = next(
        row for row in corrected["payment"]["datasets"]["payment_configurations"]
        if row["asset_id"] == "R002-payment"
    )
    payment["settings_json"]["tls_enabled"] = True
    assert import_sources(seeded, corrected)["imported_sources"] == ["payment"]
    fixed, fixed_results = scan(client, ["R002"], ["PCI-DSS"])
    fixed_result = keyed(fixed_results)[("PAY-01", "R002")]
    assert fixed_result["status"] == "pass"
    assert original_evidence_id not in {row["id"] for row in fixed_result["evidence"]}

    # Reusing an existing source hash still selects that generation as current.
    assert import_sources(seeded, original)["imported_sources"] == []
    with factory() as db:
        current = db.get(d.PaymentConfiguration, "R002-payment")
        assert current.settings_json["tls_enabled"] is False
        assert current.evidence_id == original_evidence_id

    restored, restored_results = scan(client, ["R002"], ["PCI-DSS"])
    restored_result = keyed(restored_results)[("PAY-01", "R002")]
    assert restored_result["status"] == "fail"
    assert original_evidence_id in {row["id"] for row in restored_result["evidence"]}
    with factory() as db:
        manifest = db.get(Assessment, restored["id"]).evidence_manifest
        assert manifest["R002"]["payment_configurations"]["id"] == original_evidence_id

    assert len({before["id"], fixed["id"], restored["id"]}) == 3
    for assessment, expected in [(before, before_results), (fixed, fixed_results)]:
        response = client.get(f"/api/assessments/{assessment['id']}/results")
        assert response.status_code == 200
        assert response.json()["items"] == expected


def test_historical_action_keeps_event_time_scope_after_assignment_move(client, seeded, factory):
    _, baseline_results = scan(client, ["R001"], ["GDPR"])
    assert keyed(baseline_results)[("LOY-05", "R001")]["status"] == "pass"

    # Assignment rows have no validity history. Rewrite isolated evidence because
    # the demo importer intentionally disallows deleting existing assignment keys.
    def move_current_assignment(rows):
        assignment = next(
            row for row in rows
            if row["employee_id"] == "E001-01" and row["restaurant_id"] == "R001"
        )
        assignment["restaurant_id"] = "R002"

    rewrite_snapshot(seeded, factory, "restaurant_assignments", move_current_assignment)
    _, moved_results = scan(client, ["R001"], ["GDPR"])
    moved = keyed(moved_results)[("LOY-05", "R001")]
    subjects = {row["subject_id"]: row for row in moved["details"]}
    assert subjects["EV-R001-history"]["status"] == "pass"
    assert subjects["EV-R001-read"]["status"] == "pass"

    # A still-active current grant must fail the current assignment check.
    grants = [
        row for row in load_sources()["loyalty"]["datasets"]["actual_access_grants"]
        if row["employee_id"] == "E001-01" and row["asset_id"] == "R001-loyalty" and row["active"]
    ]
    assert grants
    assert moved["status"] == "fail"
    assert all(subjects[row["id"]]["status"] == "fail" for row in grants)

    def deactivate_current_grants(rows):
        for row in rows:
            if row["employee_id"] == "E001-01" and row["asset_id"] == "R001-loyalty":
                row["active"] = False

    rewrite_snapshot(seeded, factory, "actual_access_grants", deactivate_current_grants)
    _, cleared_results = scan(client, ["R001"], ["GDPR"])
    cleared = keyed(cleared_results)[("LOY-05", "R001")]
    assert cleared["status"] == "pass"
    assert next(row for row in cleared["details"] if row["subject_id"] == "EV-R001-history")["status"] == "pass"


def test_reconsent_without_notice_does_not_clear_withdrawal(client, seeded):
    bundles = load_sources()
    events = bundles["loyalty"]["datasets"]["consent_events"]
    event = {"customer_id": "C001-02", "purpose_id": "PUR-MARKETING"}
    events.extend([
        {**event, "id": "CE-REVIEW-WITHDRAW", "event_type": "withdraw", "occurred_at": "2026-10-05T10:00:00Z", "notice_version": None},
        {**event, "id": "CE-REVIEW-INVALID-RECONSENT", "event_type": "grant", "occurred_at": "2026-10-06T10:00:00Z", "notice_version": None},
    ])
    import_sources(seeded, bundles)
    _, invalid_results = scan(client, ["R001"], ["GDPR"])
    invalid = keyed(invalid_results)[("LOY-02", "R001")]
    assert invalid["status"] == "fail"
    assert next(row for row in invalid["details"] if row["subject_id"] == "ACT-C001-02")["status"] == "fail"

    # A later valid grant before the same activity clears the withdrawal.
    events.append({**event, "id": "CE-REVIEW-VALID-RECONSENT", "event_type": "grant", "occurred_at": "2026-10-07T10:00:00Z", "notice_version": "privacy-2026-09"})
    import_sources(seeded, bundles)
    _, valid_results = scan(client, ["R001"], ["GDPR"])
    valid = keyed(valid_results)[("LOY-02", "R001")]
    assert valid["status"] == "pass"
    assert next(row for row in valid["details"] if row["subject_id"] == "ACT-C001-02")["status"] == "pass"


def test_inactive_employee_with_current_active_grant_fails(client, seeded):
    bundles = load_sources()
    employee = next(row for row in bundles["erp"]["datasets"]["employees"] if row["id"] == "E001-01")
    employee["active"] = False
    grants = [
        row for row in bundles["loyalty"]["datasets"]["actual_access_grants"]
        if row["employee_id"] == employee["id"] and row["asset_id"] == "R001-loyalty" and row["active"]
    ]
    assert grants
    assert import_sources(seeded, bundles)["imported_sources"] == ["erp"]
    _, results = scan(client, ["R001"], ["GDPR"])
    result = keyed(results)[("LOY-05", "R001")]
    assert result["status"] == "fail"
    subjects = {row["subject_id"]: row for row in result["details"]}
    assert all(subjects[row["id"]]["status"] == "fail" for row in grants)
    # Current inactivity must not invalidate historically approved actions.
    assert subjects["EV-R001-history"]["status"] == "pass"


def _import_failing_shared_payment(seeded):
    bundles = load_sources()
    asset = next(row for row in bundles["erp"]["datasets"]["assets"] if row["id"] == "R004-payment")
    asset["restaurant_id"] = None
    configuration = next(
        row for row in bundles["payment"]["datasets"]["payment_configurations"]
        if row["asset_id"] == asset["id"]
    )
    configuration["settings_json"]["tls_enabled"] = False
    import_sources(seeded, bundles)


def test_shared_payment_finding_is_deduplicated_once_per_scan(client, seeded):
    _import_failing_shared_payment(seeded)
    first, results = scan(client, frameworks=["PCI-DSS"])
    tls_results = [row for row in results if row["control_id"] == "PAY-01"]
    assert len(tls_results) == 5
    assert {row["restaurant_id"] for row in tls_results} == set(RESTAURANTS)
    for result in tls_results:
        assert result["status"] == "fail"
        shared_details = [row for row in result["details"] if row.get("asset_id") == "R004-payment"]
        assert len(shared_details) == 1
        assert shared_details[0]["status"] == "fail"

    page = findings(client, control_id="PAY-01")
    assert page["total"] == 2
    by_target = {(row["target_type"], row["target_id"]): row for row in page["items"]}
    assert set(by_target) == {("shared_system", "R004-payment"), ("restaurant", "R002")}
    shared = by_target[("shared_system", "R004-payment")]
    assert shared["restaurant_id"] is None
    assert shared["affected_restaurant_ids"] == RESTAURANTS
    assert shared["occurrences"] == 1
    private = by_target[("restaurant", "R002")]
    assert private["restaurant_id"] == "R002"
    assert private["affected_restaurant_ids"] == ["R002"]
    assert private["occurrences"] == 1

    second, _ = scan(client, frameworks=["PCI-DSS"])
    repeated = findings(client, control_id="PAY-01")
    assert repeated["total"] == 2
    assert {row["id"] for row in repeated["items"]} == {row["id"] for row in page["items"]}
    assert all(row["occurrences"] == 2 for row in repeated["items"])
    for finding in repeated["items"]:
        response = client.get(f"/api/findings/{finding['id']}")
        assert response.status_code == 200
        history = response.json()["history"]
        assert len(history) == 2
        assert {row["assessment_id"] for row in history} == {first["id"], second["id"]}

    # Filtering any affected restaurant must include the shared finding exactly once.
    for restaurant in RESTAURANTS:
        filtered = findings(client, restaurant_id=restaurant, control_id="PAY-01")
        expected = {shared["id"], private["id"]} if restaurant == "R002" else {shared["id"]}
        assert filtered["total"] == len(expected)
        assert {row["id"] for row in filtered["items"]} == expected


def test_shared_payment_subset_scan_filters_by_affected_restaurants(client, seeded):
    _import_failing_shared_payment(seeded)
    selected = ["R001", "R005"]
    _, results = scan(client, selected, ["PCI-DSS"])
    tls_results = [row for row in results if row["control_id"] == "PAY-01"]
    assert len(tls_results) == 2
    assert all(row["status"] == "fail" for row in tls_results)
    page = findings(client, control_id="PAY-01")
    assert page["total"] == 1
    shared = page["items"][0]
    assert (shared["target_type"], shared["target_id"], shared["restaurant_id"]) == ("shared_system", "R004-payment", None)
    assert shared["affected_restaurant_ids"] == selected
    assert shared["occurrences"] == 1
    for restaurant in RESTAURANTS:
        filtered = findings(client, restaurant_id=restaurant, control_id="PAY-01")
        expected = {shared["id"]} if restaurant in selected else set()
        assert filtered["total"] == len(expected)
        assert {row["id"] for row in filtered["items"]} == expected


def test_known_unauthorized_action_survives_later_unknown_employee(client, seeded, factory):
    unauthorized = next(
        row for row in load_sources(scenario="unauthorized-modification")["loyalty"]["datasets"]["employee_access_events"]
        if row["id"] == "EV-UNAUTHORIZED-001"
    )

    # A missing employee relationship cannot pass the importer's FK checks, so
    # simulate incomplete already-collected evidence with a matching content hash.
    def append_unknown_employee_action(rows):
        rows.append(unauthorized)
        rows.append({
            "id": "EV-REVIEW-UNKNOWN-EMPLOYEE",
            "employee_id": "E-REVIEW-MISSING",
            "restaurant_id": "R001",
            "asset_id": "R001-loyalty",
            "target_customer_id": "C001-02",
            "action": "customer.update",
            "occurred_at": "2026-10-22T10:00:00Z",
            "outcome": "succeeded",
        })

    rewrite_snapshot(seeded, factory, "employee_access_events", append_unknown_employee_action)
    _, results = scan(client, ["R001"], ["GDPR"])
    result = keyed(results)[("LOY-05", "R001")]
    assert result["status"] == "fail"
    subjects = {row["subject_id"]: row for row in result["details"]}
    assert subjects["EV-UNAUTHORIZED-001"]["status"] == "fail"
    assert subjects["EV-REVIEW-UNKNOWN-EMPLOYEE"]["status"] == "unknown"
    assert subjects["EV-UNAUTHORIZED-001"]["reason"]
    assert subjects["EV-REVIEW-UNKNOWN-EMPLOYEE"]["reason"]
    assert {"erp_access_approvals", "employee_access_events"} <= {row["dataset_type"] for row in result["evidence"]}
    page = findings(client, restaurant_id="R001", control_id="LOY-05")
    assert page["total"] == 1
    assert page["items"][0]["last_result_id"] == result["id"]


def test_shared_failure_does_not_rewrite_private_finding_history(client, seeded):
    first, _ = scan(client, frameworks=["PCI-DSS"])
    private = next(f for f in findings(client, control_id="PAY-01")["items"] if f["restaurant_id"] == "R002")
    bundles = load_sources()
    for asset in bundles["erp"]["datasets"]["assets"]:
        if asset["id"] == "R004-payment":
            asset["restaurant_id"] = None
    for config in bundles["payment"]["datasets"]["payment_configurations"]:
        if config["asset_id"] == "R002-payment":
            config["settings_json"]["tls_enabled"] = True
        elif config["asset_id"] == "R004-payment":
            config["settings_json"]["tls_enabled"] = False
    import_sources(seeded, bundles)
    second, results = scan(client, frameworks=["PCI-DSS"])
    assert keyed(results)[("PAY-01", "R002")]["status"] == "fail"
    current = client.get(f"/api/findings/{private['id']}").json()
    assert current["latest_evaluation_status"] == "pass"
    assert current["occurrences"] == 1
    assert [row["assessment_id"] for row in current["history"]] == [first["id"]]
    shared = next(f for f in findings(client, control_id="PAY-01")["items"] if f["target_type"] == "shared_system")
    assert shared["latest_evaluation_status"] == "fail"
    assert shared["occurrences"] == 1
    assert client.get(f"/api/findings/{shared['id']}").json()["history"][0]["assessment_id"] == second["id"]
