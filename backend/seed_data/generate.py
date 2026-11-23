"""Rebuild deterministic, fictional source fixtures. No real ERP is contacted."""
import json
from calendar import monthrange
from datetime import date
from pathlib import Path
from random import Random

ROOT = Path(__file__).resolve().parent
MONTHS = ["2026-10"]
RESTAURANTS = [f"R{i:03}" for i in range(1, 6)]
CAPTURED = "2026-11-01T12:00:00Z"
FIRST = ["Alex", "Jamie", "Morgan", "Taylor", "Casey", "Jordan", "Riley", "Avery", "Cameron", "Robin", "Drew", "Sam"]
LAST = ["Reed", "Chen", "Patel", "Diaz", "Kim"]


def build():
    erp = {k: [] for k in ["restaurants", "employees", "restaurant_assignments", "assets", "employee_payroll", "payroll_allocations", "sales_transactions", "erp_access_approvals"]}
    loyalty = {k: [] for k in ["customers", "processing_purposes", "loyalty_records", "consent_events", "visits", "customer_activities", "customer_requests", "actual_access_grants", "employee_access_events"]}
    payment = {"payment_configurations": []}
    erp["employees"].append(dict(id="E-GM", name="Alex Morgan", job_role="General Manager", active=True))
    loyalty["processing_purposes"].append(dict(id="PUR-MARKETING", name="Loyalty marketing", lawful_basis="consent", retention_days=730))
    for month in MONTHS:
        erp["employee_payroll"].append(dict(employee_id="E-GM", payroll_month=month, currency="USD", monthly_salary_minor=900000, allowance_minor=100000))
    for n, rid in enumerate(RESTAURANTS, 1):
        erp["restaurants"].append(dict(id=rid, active=True))
        erp["restaurant_assignments"].append(dict(employee_id="E-GM", restaurant_id=rid))
        for j in range(12):
            eid = f"E{n:03}-{j + 1:02}"
            role = "Manager" if j < 2 else "Staff"
            erp["employees"].append(dict(id=eid, name=f"{FIRST[j]} {LAST[n-1]}", job_role=role, active=True))
            erp["restaurant_assignments"].append(dict(employee_id=eid, restaurant_id=rid))
            for month in MONTHS:
                salary = (500000 + n * 10000 + j * 5000) if j < 2 else (280000 + n * 5000 + j * 2000)
                allowance = 40000 if j < 2 else 15000
                erp["employee_payroll"].append(dict(employee_id=eid, payroll_month=month, currency="USD", monthly_salary_minor=salary, allowance_minor=allowance))
                erp["payroll_allocations"].append(dict(employee_id=eid, payroll_month=month, restaurant_id=rid, salary_allocated_minor=salary, allowance_allocated_minor=allowance))
        for month in MONTHS:
            erp["payroll_allocations"].append(dict(employee_id="E-GM", payroll_month=month, restaurant_id=rid, salary_allocated_minor=180000, allowance_allocated_minor=20000))
        for kind in ["loyalty", "payment"]:
            erp["assets"].append(dict(id=f"{rid}-{kind}", restaurant_id=rid, owner_employee_id=f"E{n:03}-01", asset_type=kind, name=f"{rid} {kind.title()} system", active=True))
        asset = f"{rid}-loyalty"
        for eid, permissions in [("E-GM", ["customer.read", "customer.update", "customer.delete"]), (f"E{n:03}-01", ["customer.read"]), (f"E{n:03}-02", ["customer.read"])]:
            for permission in permissions:
                key = f"{rid}-{eid}-{permission.split('.')[-1]}"
                erp["erp_access_approvals"].append(dict(id=f"AP-{key}", employee_id=eid, asset_id=asset, permission=permission, status="approved", valid_from="2026-09-01T00:00:00Z", valid_until=None))
                loyalty["actual_access_grants"].append(dict(id=f"GR-{key}", employee_id=eid, asset_id=asset, permission=permission, active=True))
        # Historical update permission ended before the October read-only actions.
        erp["erp_access_approvals"].append(dict(id=f"AP-HIST-{rid}", employee_id=f"E{n:03}-01", asset_id=asset, permission="customer.update", status="approved", valid_from="2026-09-01T00:00:00Z", valid_until="2026-10-15T00:00:00Z"))
        for j in range(1, 13):
            cid = f"C{n:03}-{j:02}"
            member = j <= 10
            loyalty["customers"].append(dict(id=cid, display_name=f"Customer {n:03}-{j:02}", loyalty_member=member))
            if not member:
                continue
            record_id = f"LR-{cid}"
            loyalty["loyalty_records"].append(dict(id=record_id, customer_id=cid, restaurant_id=rid, purpose_id="PUR-MARKETING", collected_at="2026-09-01T10:00:00Z", retention_due_at="2028-08-31T10:00:00Z", status="active"))
            if not (n == 1 and j == 1):
                loyalty["consent_events"].append(dict(id=f"CE-{cid}", customer_id=cid, purpose_id="PUR-MARKETING", event_type="grant", occurred_at="2026-09-01T10:00:00Z", notice_version="privacy-2026-09"))
            loyalty["customer_activities"].append(dict(id=f"ACT-{cid}", loyalty_record_id=record_id, visit_id=None, asset_id=asset, activity_type="marketing.email", occurred_at="2026-10-10T10:00:00Z"))
        # One withdrawn customer has no later consent-based activity.
        loyalty["consent_events"].append(dict(id=f"CE-WITHDRAW-{rid}", customer_id=f"C{n:03}-10", purpose_id="PUR-MARKETING", event_type="withdraw", occurred_at="2026-10-12T10:00:00Z", notice_version=None))
        loyalty["customer_requests"].append(dict(id=f"REQ-{rid}", customer_id=f"C{n:03}-02", restaurant_id=rid, request_type="access", received_at="2026-10-05T10:00:00Z", due_at="2026-11-05T10:00:00Z", completed_at="2026-10-08T10:00:00Z", status="completed"))
        rng = Random(20261000 + n)
        for month in MONTHS:
            year, month_number = map(int, month.split("-"))
            for day in range(1, monthrange(year, month_number)[1] + 1):
                weekend = date(year, month_number, day).weekday() >= 5
                count = rng.randint(30, 45) if weekend else rng.randint(10, 20)
                for j in range(1, count + 1):
                    stamp = f"{month}-{day:02}T{rng.randint(12, 23):02}:{rng.randint(0, 59):02}:{rng.randint(0, 59):02}Z"
                    member = rng.random() < 0.5
                    cid = f"C{n:03}-{rng.randint(1, 10):02}" if member else (f"C{n:03}-{rng.randint(11, 12):02}" if rng.random() < 0.35 else None)
                    # Triple the original seeded receipts; amounts include 10% tax.
                    amount = rng.randint(1500, 12000) * 3
                    suffix = f"{rid}-{month}-{day:02}-{j:03}"
                    sale_id = f"S-{suffix}"
                    loyalty["visits"].append(dict(id=f"V-{suffix}", restaurant_id=rid, customer_id=cid, visited_at=stamp, loyalty_member_at_visit=member, payment_received_minor=amount, currency="USD", sales_transaction_id=sale_id))
                    erp["sales_transactions"].append(dict(id=sale_id, restaurant_id=rid, transaction_at=stamp, transaction_kind="sale", status="posted", amount_minor=amount, currency="USD", reference_sale_id=None))
        for suffix, action, outcome, stamp in [("read", "customer.read", "succeeded", "2026-10-20T10:00:00Z"), ("denied", "customer.update", "denied", "2026-10-20T11:00:00Z"), ("history", "customer.update", "succeeded", "2026-10-10T10:00:00Z")]:
            loyalty["employee_access_events"].append(dict(id=f"EV-{rid}-{suffix}", employee_id=f"E{n:03}-01", restaurant_id=rid, asset_id=asset, target_customer_id=f"C{n:03}-02", action=action, occurred_at=stamp, outcome=outcome))
        payment["payment_configurations"].append(dict(asset_id=f"{rid}-payment", settings_json=dict(tls_enabled=n != 2, tls_version="TLS1.3", audit_logging_enabled=n != 3, installed_patch_version="2026.10", prohibited_storage_detected=False, accounts=[dict(id=f"POS-{rid}-admin", active=True, mfa_enabled=True, password_length=16)]), policy_json=dict(allowed_tls_versions=["TLS1.2", "TLS1.3"], require_mfa=True, minimum_password_length=12, required_patch_version="2026.10")))
    return {source: dict(schema_version="1.0", source_type=source, source_version="myl-demo-v3-october-tax", captured_at=CAPTURED, restaurant_ids=RESTAURANTS, complete=True, complete_months=MONTHS, datasets=data) for source, data in [("erp", erp), ("loyalty", loyalty), ("payment", payment)]}


if __name__ == "__main__":
    bundles = build()
    visits = bundles["loyalty"]["datasets"].pop("visits")
    visit_dir = ROOT / "visits"
    visit_dir.mkdir(exist_ok=True)
    for rid in RESTAURANTS:
        rows = [row for row in visits if row["restaurant_id"] == rid]
        (visit_dir / f"{rid}-2026-10.json").write_text(json.dumps(dict(schema_version="1.0", restaurant_id=rid, month="2026-10", currency="USD", captured_at=CAPTURED, records=rows), indent=2) + "\n")
    for source, data in bundles.items():
        (ROOT / f"{source}.json").write_text(json.dumps(data, indent=2) + "\n")
    print(f"Generated exactly 5 October visit seeds ({len(visits)} visits) and 3 supporting source fixtures.")
