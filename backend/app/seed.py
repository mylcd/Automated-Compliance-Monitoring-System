"""Validate/import local ERP, loyalty and payment fixtures; no remote ERP service."""
import argparse
import hashlib
import json
from collections import defaultdict
from contextlib import nullcontext
from calendar import monthrange
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import delete, inspect, select, text, update
from sqlalchemy.dialects.sqlite import insert

from app import domain as d
from app.catalog import seed_catalog
from app.config import Settings
from app.contracts import SourceBundle, VisitSeed, validate_rows
from app.database import create_database_engine, session_factory
from app.models import User

FIXTURES = Path(__file__).resolve().parents[1] / "seed_data"
SOURCE_DATASETS = {
    "erp": list(d.DATASETS)[:8],
    "loyalty": list(d.DATASETS)[8:17],
    "payment": ["payment_configurations"],
}
IMMUTABLE = {"erp_access_approvals", "employee_access_events", "visits", "consent_events", "sales_transactions"}


def canonical(value) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, indent=2) + "\n").encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def key_for(model, row):
    return tuple(row[column.name] for column in inspect(model).primary_key)


def load_visit_seeds(source_dir, captured_at=None):
    folder = Path(source_dir) / "visits"
    expected = {f"R{i:03}-2026-10.json" for i in range(1, 6)}
    if {p.name for p in folder.glob("*.json")} != expected:
        raise ValueError("Expected exactly five visit seed files, one per restaurant for October 2026")
    rows = []
    for filename in sorted(expected):
        seed = VisitSeed.model_validate_json((folder / filename).read_bytes())
        if filename != f"{seed.restaurant_id}-{seed.month}.json" or seed.month != "2026-10":
            raise ValueError("Visit seed scope does not match its filename")
        if captured_at is not None and seed.captured_at != captured_at:
            raise ValueError("Visit seed capture time does not match the loyalty source")
        daily = defaultdict(int)
        for row in seed.records:
            if row.restaurant_id != seed.restaurant_id or row.visited_at[:7] != seed.month or row.currency != seed.currency:
                raise ValueError("Visit record is outside its restaurant/month/currency seed scope")
            daily[row.visited_at[:10]] += 1
        year, month = map(int, seed.month.split("-"))
        for day in range(1, monthrange(year, month)[1] + 1):
            stamp = f"{seed.month}-{day:02}"
            weekend = datetime.fromisoformat(stamp).weekday() >= 5
            low, high = (30, 45) if weekend else (10, 20)
            if not low <= daily[stamp] <= high:
                raise ValueError(f"{filename}: {stamp} requires {low}–{high} visits")
        rows.extend(row.model_dump() for row in seed.records)
    return rows


def snapshot_key(dataset, row=None, restaurant=None, month=None):
    if dataset == "visits":
        return dataset, restaurant or row["restaurant_id"], month or row["visited_at"][:7]
    return dataset, None, None


def load_sources(source_dir=FIXTURES, scenario="baseline"):
    bundles = {}
    for source, required in SOURCE_DATASETS.items():
        bundle = SourceBundle.model_validate_json((Path(source_dir) / f"{source}.json").read_bytes())
        if source == "loyalty":
            if "visits" in bundle.datasets:
                raise ValueError("Visit seeds must be the five separate restaurant/month files")
            bundle.datasets["visits"] = load_visit_seeds(source_dir, bundle.captured_at)
        if bundle.source_type != source or sorted(bundle.restaurant_ids) != [f"R{i:03}" for i in range(1, 6)]:
            raise ValueError(f"{source}: expected five restaurant IDs R001–R005")
        if set(bundle.datasets) != set(required):
            raise ValueError(f"{source}: datasets must be exactly {required}")
        bundle.datasets = {name: validate_rows(name, rows) for name, rows in bundle.datasets.items()}
        bundles[source] = bundle.model_dump()
    if scenario == "unauthorized-modification":
        bundles["loyalty"]["datasets"]["employee_access_events"].append(dict(id="EV-UNAUTHORIZED-001", employee_id="E001-01", restaurant_id="R001", asset_id="R001-loyalty", target_customer_id="C001-02", action="customer.update", occurred_at="2026-10-21T10:00:00Z", outcome="succeeded"))
    elif scenario == "remediated":
        bundles["loyalty"]["datasets"]["consent_events"].append(dict(id="CE-C001-01-FIX", customer_id="C001-01", purpose_id="PUR-MARKETING", event_type="grant", occurred_at="2026-09-01T10:00:00Z", notice_version="privacy-2026-09"))
        bundles["payment"]["datasets"]["payment_configurations"][1]["settings_json"]["tls_enabled"] = True
    elif scenario != "baseline":
        raise ValueError("Unknown demo scenario")
    validate_relationships(bundles)
    return bundles


def validate_relationships(bundles):
    data = {name: rows for bundle in bundles.values() for name, rows in bundle["datasets"].items()}
    indexes = {}
    for name, model in d.DATASETS.items():
        indexes[name] = {key_for(model, row): row for row in data[name]}
        if len(indexes[name]) != len(data[name]):
            raise ValueError(f"{name}: duplicate source key")
    for name, model in d.DATASETS.items():
        for row in data[name]:
            for constraint in model.__table__.foreign_key_constraints:
                if constraint.referred_table.name == "evidence":
                    continue
                values = tuple(row[element.parent.name] for element in constraint.elements)
                if all(value is None for value in values):
                    continue
                if values not in indexes[constraint.referred_table.name]:
                    raise ValueError(f"{name}: unknown {constraint.referred_table.name} reference {values}")
    if set(indexes["restaurants"]) != {(f"R{i:03}",) for i in range(1, 6)}:
        raise ValueError("The source must contain exactly five restaurants")
    gm = [row for row in data["employees"] if row["job_role"] == "General Manager"]
    if len(gm) != 1 or not gm[0]["active"]:
        raise ValueError("Exactly one active General Manager is required")
    assignments = defaultdict(set)
    for row in data["restaurant_assignments"]:
        assignments[row["employee_id"]].add(row["restaurant_id"])
    if assignments[gm[0]["id"]] != {f"R{i:03}" for i in range(1, 6)}:
        raise ValueError("The General Manager must cover all five restaurants")
    for rid in bundles["erp"]["restaurant_ids"]:
        for role, limit in [("Manager", 2), ("Staff", 10)]:
            count = sum(row["active"] and row["job_role"] == role and rid in assignments[row["id"]] for row in data["employees"])
            if count > limit:
                raise ValueError(f"{rid}: staffing exceeds {limit} {role} records")
    allocated = defaultdict(lambda: [0, 0])
    for row in data["payroll_allocations"]:
        if row["restaurant_id"] not in assignments[row["employee_id"]]:
            raise ValueError("Payroll allocation is outside employee restaurant assignments")
        sums = allocated[(row["employee_id"], row["payroll_month"])]
        sums[0] += row["salary_allocated_minor"]
        sums[1] += row["allowance_allocated_minor"]
    for row in data["employee_payroll"]:
        if allocated[(row["employee_id"], row["payroll_month"])] != [row["monthly_salary_minor"], row["allowance_minor"]]:
            raise ValueError("Payroll allocations must reconcile exactly to salary and allowance")
    for row in data["sales_transactions"]:
        if row["reference_sale_id"]:
            original = indexes["sales_transactions"][(row["reference_sale_id"],)]
            if row["transaction_kind"] != "refund" or original["transaction_kind"] != "sale" or any(row[k] != original[k] for k in ["restaurant_id", "currency"]):
                raise ValueError("Refund reference must identify a sale in the same restaurant and currency")
    visit_sales = set()
    for row in data["visits"]:
        if row["loyalty_member_at_visit"] and not row["customer_id"]:
            raise ValueError("A loyalty-member visit must identify its customer")
        sale = indexes["sales_transactions"][(row["sales_transaction_id"],)]
        if row["sales_transaction_id"] in visit_sales:
            raise ValueError("A sale cannot be counted as payment for more than one visit")
        visit_sales.add(row["sales_transaction_id"])
        if sale["transaction_kind"] != "sale" or sale["status"] != "posted" or sale["amount_minor"] != row["payment_received_minor"] or sale["currency"] != row["currency"] or sale["restaurant_id"] != row["restaurant_id"] or sale["transaction_at"] != row["visited_at"]:
            raise ValueError("Visit payment must reconcile to its posted sale, restaurant, currency, and time")
        if row["visited_at"][:7] not in bundles["loyalty"]["complete_months"]:
            raise ValueError("Visit month is outside the declared source period")
    if visit_sales != {s["id"] for s in data["sales_transactions"] if s["transaction_kind"] == "sale" and s["status"] == "posted"}:
        raise ValueError("Every posted demo sale must match exactly one visit payment")
    for name in ["employee_access_events", "payment_configurations", "customer_activities"]:
        for row in data[name]:
            asset = indexes["assets"][(row["asset_id"],)]
            if name == "employee_access_events" and asset["restaurant_id"] not in (None, row["restaurant_id"]):
                raise ValueError("Action restaurant does not match its system")
            if name == "payment_configurations" and asset["asset_type"] != "payment":
                raise ValueError("Payment configuration requires a payment asset")
            if name == "customer_activities":
                record = indexes["loyalty_records"][(row["loyalty_record_id"],)]
                if asset["asset_type"] != "loyalty" or asset["restaurant_id"] not in (None, record["restaurant_id"]):
                    raise ValueError("Customer activity must reference its loyalty system")
                if row["visit_id"]:
                    visit = indexes["visits"][(row["visit_id"],)]
                    if visit["customer_id"] != record["customer_id"] or visit["restaurant_id"] != record["restaurant_id"]:
                        raise ValueError("Activity visit does not match customer/restaurant")
    for row in data["erp_access_approvals"]:
        if row["valid_until"] and row["valid_until"] <= row["valid_from"]:
            raise ValueError("Approval validity end must follow its start")


def safe_evidence_path(root: Path, relative_path: str) -> Path:
    root = root.resolve()
    target = (root / relative_path).resolve()
    if not target.is_relative_to(root) or target == root:
        raise ValueError("Evidence path escapes the configured directory")
    return target


def current_source_runs(db):
    """Selected generations follow normalized evidence references, including re-imports."""
    selected = {}
    for source, datasets in SOURCE_DATASETS.items():
        runs = set()
        for dataset in datasets:
            model = d.DATASETS[dataset]
            runs.update(db.scalars(select(d.Evidence.collection_run_id).join(model, model.evidence_id == d.Evidence.id).distinct()))
        if len(runs) == 1:
            selected[source] = next(iter(runs))
        elif not runs:
            selected[source] = db.scalar(select(d.CollectionRun.id).where(d.CollectionRun.source_type == source).order_by(d.CollectionRun.imported_at.desc(), d.CollectionRun.id.desc()).limit(1))
        else:
            selected[source] = None
    return selected


def import_sources(settings: Settings, bundles, *, db=None, created_files=None) -> dict:
    normalized = {}
    if set(bundles) != set(SOURCE_DATASETS):
        raise ValueError("Provide ERP, loyalty, and payment source snapshots together")
    for source, value in bundles.items():
        bundle = SourceBundle.model_validate(value)
        if bundle.source_type != source or set(bundle.datasets) != set(SOURCE_DATASETS[source]):
            raise ValueError(f"{source}: source type or datasets do not match the connector contract")
        if sorted(bundle.restaurant_ids) != [f"R{i:03}" for i in range(1, 6)]:
            raise ValueError(f"{source}: expected five restaurant IDs R001–R005")
        bundle.datasets = {name: validate_rows(name, rows) for name, rows in bundle.datasets.items()}
        normalized[source] = bundle.model_dump()
    bundles = normalized
    validate_relationships(bundles)
    engine = create_database_engine(settings.database_url) if db is None else None
    created_files = created_files if created_files is not None else []
    imported = []
    try:
        with (session_factory(engine).begin() if engine is not None else nullcontext(db)) as db:
            users = list(db.scalars(select(User)))
            if len(users) != 1:
                raise ValueError("Initialize exactly one administrator before importing")
            # Validate historical changes and missing rows before writing anything.
            for bundle in bundles.values():
                for name, rows in bundle["datasets"].items():
                    model = d.DATASETS[name]
                    incoming = {key_for(model, row): row for row in rows}
                    for existing in db.scalars(select(model)):
                        old = {column.name: getattr(existing, column.name) for column in inspect(model).columns if column.name != "evidence_id"}
                        key = key_for(model, old)
                        if key not in incoming:
                            raise ValueError(f"{name}: snapshot omitted existing records; use explicit inactive states or reset the demo")
                        if name in IMMUTABLE and old != incoming[key]:
                            raise ValueError(f"{name}: historical records are immutable; append a new source event/approval ID")
            evidence_ids = {}
            for source, bundle in bundles.items():
                source_hash = digest(canonical(bundle))
                run_id = f"{source}-{source_hash}"
                existing_run = db.get(d.CollectionRun, run_id)
                if existing_run:
                    # Do not silently skip missing/tampered evidence files.
                    for item in db.scalars(select(d.Evidence).where(d.Evidence.collection_run_id == run_id)):
                        path = safe_evidence_path(settings.evidence_dir, item.relative_path)
                        if not path.is_file() or digest(path.read_bytes()) != item.sha256:
                            raise ValueError("Existing evidence is missing or modified; investigate before re-importing")
                        evidence_ids[snapshot_key(item.dataset_type, restaurant=item.restaurant_ids[0] if item.dataset_type == "visits" else None, month=item.period_month)] = item.id
                    continue
                db.add(d.CollectionRun(id=run_id, source_type=source, source_version=bundle["source_version"], source_hash=source_hash, captured_at=bundle["captured_at"], imported_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), status="completed" if bundle["complete"] else "incomplete", restaurant_ids=bundle["restaurant_ids"]))
                db.flush()
                parts = []
                for name, rows in bundle["datasets"].items():
                    if name == "visits":
                        grouped = defaultdict(list)
                        for row in rows:
                            grouped[(row["restaurant_id"], row["visited_at"][:7])].append(row)
                        parts.extend((name, records, [rid], month, f"visits/{rid}-{month}") for (rid, month), records in sorted(grouped.items()))
                    else:
                        parts.append((name, rows, bundle["restaurant_ids"], None, name))
                for name, rows, scope, month, suffix in parts:
                    payload = {k: v for k, v in bundle.items() if k != "datasets"}
                    payload.update(dataset_type=name, records=rows, restaurant_ids=scope)
                    if month:
                        payload.update(month=month, complete_months=[month], seed_file=f"{suffix}.json")
                    contents = canonical(payload)
                    relative = f"phase2/{source}/{source_hash}/{suffix}.json"
                    path = safe_evidence_path(settings.evidence_dir, relative)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if path.exists():
                        if path.read_bytes() != contents:
                            raise ValueError("Evidence snapshot collision or modification")
                    else:
                        with path.open("xb") as output:
                            created_files.append(path)
                            output.write(contents)
                    evidence_id = f"{run_id}-{suffix.replace('/', '-')}"
                    evidence_ids[snapshot_key(name, restaurant=scope[0], month=month)] = evidence_id
                    db.add(d.Evidence(id=evidence_id, collection_run_id=run_id, dataset_type=name, captured_at=bundle["captured_at"], restaurant_ids=scope, asset_ids=sorted({row["asset_id"] for row in rows if row.get("asset_id")}), relative_path=relative, sha256=digest(contents), record_count=len(rows), complete=bundle["complete"], period_month=month))
                imported.append(source)
            db.flush()
            data = {name: rows for bundle in bundles.values() for name, rows in bundle["datasets"].items()}
            for name, model in d.DATASETS.items():
                rows = data[name]
                if name == "sales_transactions":
                    rows = sorted(rows, key=lambda row: row["reference_sale_id"] is not None)
                for start in range(0, len(rows), 50):
                    batch = [{**row, "evidence_id": evidence_ids[snapshot_key(name, row)]} for row in rows[start:start + 50]]
                    statement = insert(model).values(batch)
                    keys = [column.name for column in inspect(model).primary_key]
                    db.execute(statement.on_conflict_do_update(index_elements=keys, set_={column.name: statement.excluded[column.name] for column in inspect(model).columns if column.name not in keys}))
                db.flush()
            gm = next(row for row in data["employees"] if row["job_role"] == "General Manager" and row["active"])
            users[0].employee_id = gm["id"]
            seed_catalog(db)
        return {"imported_sources": imported, "unchanged_sources": [s for s in bundles if s not in imported], "records": {k: len(v) for k, v in data.items()}}
    except Exception:
        for path in created_files:
            path.unlink(missing_ok=True)
        raise
    finally:
        if engine is not None:
            engine.dispose()


def reset_demo(settings: Settings) -> None:
    from app.assessment_models import Assessment, AssessmentResult, Finding, ResultEvidence
    from app.remediation_models import AuditEvent, MockSourceRevision
    from app.remediation import source_revision_path
    from app.monitoring_models import ScanSchedule
    import shutil
    engine = create_database_engine(settings.database_url)
    try:
        with session_factory(engine).begin() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            if db.scalar(select(Assessment.id).where(Assessment.active_slot == 1)):
                raise ValueError("Cannot reset demo data while an assessment is queued or running")
            paths = [item.relative_path for item in db.scalars(select(d.Evidence))]
            # Validate exact managed targets before any database deletion.
            targets = [safe_evidence_path(settings.evidence_dir, p) for p in paths]
            if any(not p.startswith("phase2/") for p in paths):
                raise ValueError("Reset refused: unmanaged evidence exists")
            source_folders = [source_revision_path(settings, item.relative_path) for item in db.scalars(select(MockSourceRevision))]
            schedule = db.get(ScanSchedule, 1)
            if schedule and schedule.enabled:
                raise ValueError("Disable scheduled scans before resetting demo data")
            if schedule:
                schedule.last_assessment_id = schedule.last_run_at = schedule.last_error = schedule.next_run_at = None
                schedule.restaurant_ids = []
                schedule.revision += 1
            db.execute(update(User).values(employee_id=None))
            for model in [AuditEvent, MockSourceRevision, Finding, ResultEvidence, AssessmentResult, Assessment]:
                db.execute(delete(model))
            db.execute(delete(d.ControlEvidence))
            for model in reversed(list(d.DATASETS.values())):
                if model is d.SalesTransaction:
                    db.execute(delete(model).where(model.reference_sale_id.is_not(None)))
                db.execute(delete(model))
            for model in [d.ControlMapping, d.Control, d.Framework, d.Evidence, d.CollectionRun]:
                db.execute(delete(model))
        for target in targets:
            target.unlink(missing_ok=True)
        for folder in source_folders:
            if folder.is_dir():
                shutil.rmtree(folder)
        # Reset only our generated connector; immutable fixture files stay intact.
        from app.monitoring import connector_path
        connector = connector_path(settings)
        if connector.is_dir():
            shutil.rmtree(connector)
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "import", "reset"])
    parser.add_argument("--source-dir", type=Path, default=FIXTURES)
    parser.add_argument("--scenario", choices=["baseline", "remediated", "unauthorized-modification"], default="baseline")
    parser.add_argument("--yes", action="store_true", help="Confirm reset of generated demo domain data/evidence")
    args = parser.parse_args()
    try:
        if args.command == "check":
            load_sources(args.source_dir, args.scenario)
            print("Source contracts and relationships are valid.")
            return
        from app.bootstrap import bootstrap
        settings = Settings()
        bootstrap(settings)
        if args.command == "reset":
            if not args.yes:
                parser.error("reset requires --yes; administrator and sessions will be preserved")
            reset_demo(settings)
            print("Reset generated demo records/evidence; administrator and sessions preserved.")
        else:
            print(json.dumps(import_sources(settings, load_sources(args.source_dir, args.scenario)), indent=2))
    except (ValueError, ValidationError, OSError) as error:
        parser.exit(2, f"Import failed: {error}\n")


if __name__ == "__main__":
    main()
