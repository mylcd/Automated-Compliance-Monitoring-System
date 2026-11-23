from copy import deepcopy
from collections import Counter
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import shutil

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app import domain as d
from app.bootstrap import BACKEND_DIR, bootstrap
from app.config import Settings
from app.database import create_database_engine, session_factory
from app.main import COOKIE_NAME, create_app, hash_token
from app.models import LoginSession, User
from app.inventory import sales_before_tax
from app.seed import FIXTURES, import_sources, load_sources, load_visit_seeds, reset_demo

PASSWORD = "phase-two-test-password"
HEADERS = {"X-Requested-With": "XMLHttpRequest", "Origin": "http://testserver"}


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, admin_username="admin", admin_password=PASSWORD, database_url=f"sqlite:///{tmp_path / 'demo.db'}", evidence_dir=tmp_path / "evidence", app_origin="http://testserver", seed_demo_data=False)


@pytest.fixture
def seeded(settings):
    bootstrap(settings)
    import_sources(settings, load_sources())
    return settings


@pytest.fixture
def client(seeded):
    with TestClient(create_app(seeded)) as client:
        assert client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD}, headers=HEADERS).status_code == 200
        yield client


def test_seed_counts_and_repeat_import_preserves_snapshots(seeded):
    engine = create_database_engine(seeded.database_url)
    first_files = {p: p.read_bytes() for p in seeded.evidence_dir.rglob("*.json")}
    result = import_sources(seeded, load_sources())
    assert result["imported_sources"] == []
    assert set(result["unchanged_sources"]) == {"erp", "loyalty", "payment"}
    assert first_files == {p: p.read_bytes() for p in seeded.evidence_dir.rglob("*.json")}
    with session_factory(engine)() as db:
        for model, count in [(d.Restaurant, 5), (d.Employee, 61), (d.Visit, 3375), (d.EmployeePayroll, 61), (d.SalesTransaction, 3375), (d.Control, 10), (d.CollectionRun, 3), (d.Evidence, 22), (User, 1)]:
            assert db.scalar(select(func.count()).select_from(model)) == count
        assert db.scalar(select(User)).employee_id == "E-GM"
    engine.dispose()


def test_restaurants_employees_visits_and_pagination(client):
    restaurants = client.get("/api/restaurants").json()["items"]
    assert [r["id"] for r in restaurants] == [f"R{i:03}" for i in range(1, 6)]
    assert all(r["employee_count"] == 13 and 490 <= r["visit_count"] <= 845 and r["assessment_status"] == "not_assessed" for r in restaurants)
    assert sum(r["visit_count"] for r in restaurants) == 3375
    staff = client.get("/api/employees?restaurant_id=R001&role=Staff").json()
    assert staff["total"] == 10 and all(not e["platform_access"] for e in staff["items"])
    assert client.get("/api/employees/E-GM").json()["platform_access"] is True
    assert len(client.get("/api/employees/E-GM").json()["allocations"]) == 5
    visits = client.get("/api/visits?restaurant_id=R003&month=2026-10&member=false&limit=7&offset=7").json()
    assert visits["total"] > 100 and len(visits["items"]) == 7
    assert all(v["restaurant_id"] == "R003" and not v["loyalty_member_at_visit"] for v in visits["items"])
    assert client.get("/api/restaurants/R050").status_code == 404
    assert client.get("/api/visits?month=2026-13").status_code == 422
    assert client.get("/api/data/users").status_code == 404
    assert client.get("/api/data/restaurants?restaurant_id=R001").json()["total"] == 1
    assert client.get("/api/data/employees?restaurant_id=R001").json()["total"] == 13
    assert client.get("/api/data/customers?restaurant_id=R001").json()["total"] == 12


def test_summary_matches_source_and_does_not_duplicate_gm(client):
    source = load_sources()["erp"]["datasets"]
    result = client.get("/api/resource-planning/summary?month=2026-10").json()
    assert result["complete"] is True
    assert len(result["restaurants"]) == 5
    posted = [s for s in source["sales_transactions"] if s["status"] == "posted" and s["transaction_at"].startswith("2026-10")]
    gross = sum(s["amount_minor"] * (-1 if s["transaction_kind"] == "refund" else 1) for s in posted)
    sales = sum(int((Decimal(s["amount_minor"]) / Decimal("1.10")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) * (-1 if s["transaction_kind"] == "refund" else 1) for s in posted)
    payroll = sum(p["monthly_salary_minor"] + p["allowance_minor"] for p in source["employee_payroll"] if p["payroll_month"] == "2026-10")
    assert result["tax_rate_percent"] == 10
    assert result["totals"] == [{"currency": "USD", "total_sales_minor": sales, "total_payroll_minor": payroll, "total_payments_minor": gross, "total_tax_minor": gross - sales}]
    assert sum(r["totals"][0]["total_payroll_minor"] for r in result["restaurants"]) == payroll
    for row in result["restaurants"]:
        allocations = [a for a in source["payroll_allocations"] if a["restaurant_id"] == row["restaurant_id"] and a["payroll_month"] == "2026-10"]
        assert row["totals"][0]["total_payroll_minor"] == sum(a["salary_allocated_minor"] + a["allowance_allocated_minor"] for a in allocations)
    missing = client.get("/api/resource-planning/summary?month=2026-12").json()
    assert not missing["complete"] and missing["totals"][0]["total_sales_minor"] is None


def test_evidence_preview_and_download_are_protected(client):
    evidence = client.get("/api/evidence?dataset=employee_payroll").json()["items"][0]
    path = f"/api/evidence/{evidence['id']}/content"
    payload = client.get(path).json()
    assert len(payload["records"]) == 61
    assert payload["complete_months"] == ["2026-10"]
    assert "attachment" in client.get(path + "?download=true").headers["content-disposition"]
    client.cookies.clear()
    for url in ["/api/employees", "/api/restaurants", "/api/visits", "/api/resource-planning/summary", path]:
        assert client.get(url).status_code == 401


def test_import_preserves_historical_evidence_and_separates_actions(seeded):
    prior = {p: p.read_bytes() for p in seeded.evidence_dir.rglob("*.json")}
    result = import_sources(seeded, load_sources(scenario="unauthorized-modification"))
    assert result["imported_sources"] == ["loyalty"]
    assert all(path.read_bytes() == content for path, content in prior.items())
    engine = create_database_engine(seeded.database_url)
    with session_factory(engine)() as db:
        assert db.get(d.EmployeeAccessEvent, "EV-UNAUTHORIZED-001").outcome == "succeeded"
        assert db.get(d.EmployeeAccessEvent, "EV-R001-denied").outcome == "denied"
        assert db.get(d.AccessApproval, "AP-HIST-R001").valid_until == "2026-10-15T00:00:00Z"
        assert db.scalar(select(func.count()).select_from(d.CollectionRun)) == 4
    engine.dispose()
    edited = load_sources(scenario="unauthorized-modification")
    edited["loyalty"]["datasets"]["employee_access_events"][0]["outcome"] = "denied"
    with pytest.raises(ValueError, match="immutable"):
        import_sources(seeded, edited)


@pytest.mark.parametrize("mutation,match", [
    (lambda b: b["erp"]["datasets"]["payroll_allocations"][0].update(salary_allocated_minor=1), "reconcile"),
    (lambda b: next(v for v in b["loyalty"]["datasets"]["visits"] if v["loyalty_member_at_visit"]).update(customer_id=None), "identify"),
    (lambda b: b["loyalty"]["datasets"]["employee_access_events"][0].update(employee_id="unknown"), "unknown"),
    (lambda b: b["erp"]["datasets"]["restaurants"].append(deepcopy(b["erp"]["datasets"]["restaurants"][0])), "duplicate"),
])
def test_invalid_import_is_atomic(seeded, mutation, match):
    bundles = load_sources()
    mutation(bundles)
    previous_files = set(seeded.evidence_dir.rglob("*.json"))
    with pytest.raises(ValueError, match=match):
        import_sources(seeded, bundles)
    assert set(seeded.evidence_dir.rglob("*.json")) == previous_files
    engine = create_database_engine(seeded.database_url)
    with session_factory(engine)() as db:
        assert db.scalar(select(func.count()).select_from(d.Restaurant)) == 5
    engine.dispose()


def test_reset_preserves_admin_password_and_session(client, seeded):
    token = client.cookies.get(COOKIE_NAME)
    unrelated = seeded.evidence_dir / "keep.txt"
    unrelated.write_text("Unmanaged file")
    reset_demo(seeded)
    assert unrelated.read_text() == "Unmanaged file"
    assert not list(seeded.evidence_dir.rglob("*.json"))
    assert client.get("/api/auth/me").status_code == 200
    assert client.get("/api/restaurants").json()["total"] == 0
    import_sources(seeded, load_sources())
    assert client.get("/api/auth/me").json()["employee_id"] == "E-GM"
    engine = create_database_engine(seeded.database_url)
    with session_factory(engine)() as db:
        assert db.get(LoginSession, hash_token(token)) is not None
        assert db.scalar(select(func.count()).select_from(User)) == 1
    engine.dispose()


def test_snapshot_tampering_and_path_escape_are_denied(client, seeded, tmp_path):
    engine = create_database_engine(seeded.database_url)
    with session_factory(engine).begin() as db:
        item = db.scalar(select(d.Evidence))
        evidence_id = item.id
        snapshot = seeded.evidence_dir / item.relative_path
        original = snapshot.read_bytes()
        snapshot.write_text("{}")
    assert client.get(f"/api/evidence/{evidence_id}/content").status_code == 409
    with pytest.raises(ValueError, match="modified"):
        import_sources(seeded, load_sources())
    snapshot.write_bytes(original)
    outside = tmp_path / "private.json"
    outside.write_text('{"secret":"not evidence"}')
    with session_factory(engine).begin() as db:
        db.get(d.Evidence, evidence_id).relative_path = "../private.json"
    assert client.get(f"/api/evidence/{evidence_id}/content").status_code == 404
    engine.dispose()


def test_manager_cannot_gain_platform_access(client, seeded):
    engine = create_database_engine(seeded.database_url)
    with session_factory(engine).begin() as db:
        db.scalar(select(User)).employee_id = "E001-01"
    assert client.get("/api/employees").status_code == 403
    assert client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD}, headers=HEADERS).status_code == 403
    engine.dispose()


def test_existing_phase_one_credentials_and_sessions_survive_migration(settings):
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.attributes["database_url"] = settings.database_url
    command.upgrade(config, "0001")
    engine = create_database_engine(settings.database_url)
    with engine.begin() as db:
        db.execute(text("INSERT INTO users (id, username, password_hash) VALUES (1, 'old-admin', 'existing-hash')"))
        db.execute(text("INSERT INTO login_sessions VALUES ('existing-token', 1, 'existing-csrf', 9999999999)"))
    bootstrap(settings)
    with engine.connect() as db:
        assert db.execute(text("SELECT username, password_hash FROM users")).one() == ("old-admin", "existing-hash")
        assert db.execute(text("SELECT token_hash FROM login_sessions")).scalar() == "existing-token"
        assert db.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()


def test_summary_keeps_currencies_separate(client, seeded):
    bundle = load_sources()
    for payroll in bundle["erp"]["datasets"]["employee_payroll"]:
        if payroll["employee_id"] == "E-GM":
            payroll["currency"] = "EUR"
    import_sources(seeded, bundle)
    result = client.get("/api/resource-planning/summary?month=2026-10").json()
    totals = {r["currency"]: r for r in result["totals"]}
    assert set(totals) == {"USD", "EUR"}
    assert totals["EUR"]["total_payroll_minor"] == 1000000
    assert totals["EUR"]["total_sales_minor"] == 0
    assert totals["USD"]["total_payroll_minor"] > 0


def test_incomplete_import_does_not_report_payroll_zero(client, seeded):
    bundle = load_sources()
    bundle["erp"]["complete"] = False
    import_sources(seeded, bundle)
    result = client.get("/api/resource-planning/summary?month=2026-10").json()
    assert result["complete"] is False
    assert result["totals"][0]["total_sales_minor"] is None
    assert result["totals"][0]["total_payroll_minor"] is None


@pytest.mark.parametrize("dataset,field,value", [
    ("employees", "job_role", "Director"),
    ("employee_payroll", "monthly_salary_minor", -1),
    ("employee_payroll", "monthly_salary_minor", 123.45),
    ("employee_payroll", "currency", "dollars"),
])
def test_source_contracts_reject_invalid_role_or_money(seeded, dataset, field, value):
    bundle = load_sources()
    bundle["erp"]["datasets"][dataset][0][field] = value
    with pytest.raises(ValueError):
        import_sources(seeded, bundle)


def test_auto_seed_happens_only_on_empty_inventory(settings):
    auto = settings.model_copy(update={"seed_demo_data": True})
    with TestClient(create_app(auto)) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD}, headers=HEADERS)
        assert client.get("/api/overview").json()["employees"] == 61
    engine = create_database_engine(auto.database_url)
    with session_factory(engine).begin() as db:
        db.get(d.Employee, "E001-01").name = "Kept local edit"
    with TestClient(create_app(auto)):
        pass
    with session_factory(engine)() as db:
        assert db.get(d.Employee, "E001-01").name == "Kept local edit"
    engine.dispose()


def test_exactly_five_october_visit_seeds_meet_every_daily_range():
    files = sorted((FIXTURES / "visits").glob("*.json"))
    assert [p.name for p in files] == [f"R{i:03}-2026-10.json" for i in range(1, 6)]
    assert "visits" not in json.loads((FIXTURES / "loyalty.json").read_text())["datasets"]
    for index, path in enumerate(files, 1):
        seed = json.loads(path.read_text())
        assert seed["restaurant_id"] == f"R{index:03}" and seed["month"] == "2026-10"
        counts = Counter(v["visited_at"][:10] for v in seed["records"])
        assert len(counts) == 31
        for day in range(1, 32):
            stamp = f"2026-10-{day:02}"
            low, high = (30, 45) if date(2026, 10, day).weekday() >= 5 else (10, 20)
            assert low <= counts[stamp] <= high
        assert all(v["restaurant_id"] == seed["restaurant_id"] and v["currency"] == "USD" and isinstance(v["payment_received_minor"], int) and v["payment_received_minor"] > 0 for v in seed["records"])
        assert len({counts[f"2026-10-{day:02}"] for day in range(1, 32)}) > 5


def test_visit_loader_rejects_out_of_scope_or_missing_files(tmp_path):
    shutil.copytree(FIXTURES / "visits", tmp_path / "visits")
    target = tmp_path / "visits" / "R001-2026-10.json"
    payload = json.loads(target.read_text())
    payload["records"][0]["restaurant_id"] = "R002"
    target.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="scope"):
        load_visit_seeds(tmp_path)
    target.unlink()
    with pytest.raises(ValueError, match="exactly five"):
        load_visit_seeds(tmp_path)


def test_visit_payments_daily_rows_and_five_evidence_snapshots_reconcile(client):
    bundles = load_sources()
    visits = bundles["loyalty"]["datasets"]["visits"]
    summary = client.get("/api/resource-planning/summary?month=2026-10").json()
    total = summary["totals"][0]
    assert total["total_payments_minor"] == sum(v["payment_received_minor"] for v in visits)
    assert total["total_payments_minor"] == 68588172
    assert total["total_sales_minor"] + total["total_tax_minor"] == total["total_payments_minor"]
    evidence = client.get("/api/evidence?dataset=visits&month=2026-10").json()
    assert evidence["total"] == 5
    assert sum(e["record_count"] for e in evidence["items"]) == len(visits)
    days = client.get("/api/visits/days?month=2026-10&limit=200").json()
    assert days["total"] == 155 and days["visits_total"] == 3375
    assert sum(r["payments"]["USD"] for r in days["items"]) == total["total_payments_minor"]
    for key in ["total_payments_minor", "total_tax_minor", "total_sales_minor", "total_payroll_minor"]:
        assert sum(r["totals"][0][key] for r in summary["restaurants"]) == total[key]
    for restaurant in summary["restaurants"]:
        rid = restaurant["restaurant_id"]
        amounts = restaurant["totals"][0]
        assert amounts["total_payments_minor"] == sum(v["payment_received_minor"] for v in visits if v["restaurant_id"] == rid)
        assert amounts["total_sales_minor"] + amounts["total_tax_minor"] == amounts["total_payments_minor"]
    for snapshot in evidence["items"]:
        assert len(snapshot["restaurant_ids"]) == 1 and snapshot["period_month"] == "2026-10"
        payload = client.get(f"/api/evidence/{snapshot['id']}/content").json()
        assert {v["restaurant_id"] for v in payload["records"]} == set(snapshot["restaurant_ids"])
        assert {v["visited_at"][:7] for v in payload["records"]} == {"2026-10"}
    selected = next(row for row in days["items"] if row["id"] == "V-R002-2026-10-15")
    monthly = client.get(f"/api/evidence/{selected['evidence_id']}/content").json()
    highlighted = [v for v in monthly["records"] if v["visited_at"].startswith(selected["visit_date"])]
    assert len(highlighted) == selected["visit_count"]
    assert sum(v["payment_received_minor"] for v in highlighted) == selected["payments"]["USD"]
    assert client.get("/api/visits?month=2026-11").json()["total"] == 0
    assert client.get("/api/resource-planning/summary?month=2026-11").json()["complete"] is False


@pytest.mark.parametrize("field,value", [("payment_received_minor", 1), ("sales_transaction_id", "unknown"), ("currency", "EUR")])
def test_unreconciled_visit_payments_are_rejected(seeded, field, value):
    bundles = load_sources()
    bundles["loyalty"]["datasets"]["visits"][0][field] = value
    with pytest.raises(ValueError):
        import_sources(seeded, bundles)


@pytest.mark.parametrize("gross,net", [(0, 0), (11000, 10000), (4500, 4091), (5, 5), (6, 5), (55, 50)])
def test_inclusive_tax_rounding(gross, net):
    assert sales_before_tax(gross) == net


def test_refunds_reduce_gross_net_and_tax_and_voids_are_excluded(client, seeded):
    before = client.get("/api/resource-planning/summary?month=2026-10").json()["totals"][0]
    bundles = load_sources()
    sales = bundles["erp"]["datasets"]["sales_transactions"]
    original = sales[0]
    sales.append({**original, "id": "REFUND-TAX-TEST", "amount_minor": 1100, "transaction_kind": "refund", "reference_sale_id": original["id"]})
    sales.append({**original, "id": "VOID-TAX-TEST", "amount_minor": 999999, "status": "void"})
    import_sources(seeded, bundles)
    after = client.get("/api/resource-planning/summary?month=2026-10").json()["totals"][0]
    assert after["total_payments_minor"] == before["total_payments_minor"] - 1100
    assert after["total_sales_minor"] == before["total_sales_minor"] - 1000
    assert after["total_tax_minor"] == before["total_tax_minor"] - 100
    assert after["total_payroll_minor"] == before["total_payroll_minor"]
