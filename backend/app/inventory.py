"""Read-only inventory and resource planning endpoints for the General Manager."""
import json
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, inspect, select, text

from app import domain as d
from app.seed import digest, safe_evidence_path
from app.assessment_models import Assessment, AssessmentResult, Finding
from app.assessments import assessment_dict, latest_result


TAX_RATE_PERCENT = 10


def sales_before_tax(payment_minor):
    """Remove inclusive demo tax, rounding each transaction to the nearest cent."""
    return int((Decimal(payment_minor) / Decimal("1.10")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def record(value):
    return {column.name: getattr(value, column.name) for column in inspect(type(value)).columns}


def evidence_payload(settings, evidence):
    try:
        path = safe_evidence_path(settings.evidence_dir, evidence.relative_path)
        contents = path.read_bytes()
        if digest(contents) != evidence.sha256:
            raise HTTPException(409, "Evidence hash does not match the stored snapshot")
        return json.loads(contents), path
    except (OSError, ValueError):
        raise HTTPException(404, "Evidence snapshot is unavailable") from None


def planning_summary(db, settings, month):
    restaurants = list(db.scalars(select(d.Restaurant).order_by(d.Restaurant.id)))
    payroll = list(db.scalars(select(d.EmployeePayroll)))
    sales = list(db.scalars(select(d.SalesTransaction)))
    month = month or max((p.payroll_month for p in payroll), default=None)
    currencies = sorted({p.currency for p in payroll} | {s.currency for s in sales})
    latest = {}
    for item in db.scalars(select(d.Evidence).join(d.CollectionRun).order_by(d.CollectionRun.imported_at.desc(), d.Evidence.id)):
        latest.setdefault(item.dataset_type, item)
    source_meta = {}
    for dataset in ["sales_transactions", "employee_payroll", "payroll_allocations"]:
        current = list(db.scalars(select(d.DATASETS[dataset].evidence_id).distinct()))
        item = db.get(d.Evidence, current[0]) if len(current) == 1 else latest.get(dataset) if not current else None
        payload = None
        if item:
            try:
                payload, _ = evidence_payload(settings, item)
            except HTTPException:
                pass
        source_meta[dataset] = {"evidence_id": item.id if item else None, "captured_at": item.captured_at if item else None, "restaurant_ids": item.restaurant_ids if item else [], "available": bool(item and payload and item.complete and month in payload["complete_months"])}
    amounts = defaultdict(lambda: [0, 0, 0, 0])
    for sale in sales:
        if sale.transaction_at[:7] == month and sale.status == "posted":
            sign = -1 if sale.transaction_kind == "refund" else 1
            net = sales_before_tax(sale.amount_minor)
            values = amounts[(sale.restaurant_id, sale.currency)]
            values[0] += net * sign
            values[2] += sale.amount_minor * sign
            values[3] += (sale.amount_minor - net) * sign
    periods = {(p.employee_id, p.payroll_month): p for p in payroll}
    for allocation in db.scalars(select(d.PayrollAllocation).where(d.PayrollAllocation.payroll_month == month)):
        parent = periods[(allocation.employee_id, allocation.payroll_month)]
        amounts[(allocation.restaurant_id, parent.currency)][1] += allocation.salary_allocated_minor + allocation.allowance_allocated_minor
    items = []
    for restaurant in restaurants:
        sales_ok = source_meta["sales_transactions"]["available"] and restaurant.id in source_meta["sales_transactions"]["restaurant_ids"]
        payroll_ok = all(source_meta[k]["available"] and restaurant.id in source_meta[k]["restaurant_ids"] for k in ["employee_payroll", "payroll_allocations"])
        items.append({"restaurant_id": restaurant.id, "complete": sales_ok and payroll_ok, "totals": [{"currency": c, "total_sales_minor": amounts[(restaurant.id, c)][0] if sales_ok else None, "total_payroll_minor": amounts[(restaurant.id, c)][1] if payroll_ok else None, "total_payments_minor": amounts[(restaurant.id, c)][2] if sales_ok else None, "total_tax_minor": amounts[(restaurant.id, c)][3] if sales_ok else None} for c in currencies]})
    totals = []
    for currency in currencies:
        values = [next(v for v in row["totals"] if v["currency"] == currency) for row in items]
        totals.append({"currency": currency, **{key: sum(v[key] for v in values) if values and all(v[key] is not None for v in values) else None for key in ["total_sales_minor", "total_payroll_minor", "total_payments_minor", "total_tax_minor"]}})
    return {"month": month, "as_of": min((m["captured_at"] for m in source_meta.values() if m["captured_at"]), default=None), "tax_rate_percent": TAX_RATE_PERCENT, "sales_basis": "Payment received / 1.10; rounded half up per transaction to cents; posted refunds deducted", "payroll_basis": "Monthly salary plus allowance; not a payment confirmation", "complete": len(items) == 5 and all(row["complete"] for row in items), "restaurants": items, "totals": totals, "sources": source_meta}


def create_inventory_router(database, current_session, settings):
    router = APIRouter(prefix="/api", dependencies=[Depends(current_session)])

    def ensure_restaurant(db, restaurant_id):
        if restaurant_id and db.get(d.Restaurant, restaurant_id) is None:
            raise HTTPException(404, "Restaurant not found")

    def page(db, statement, limit, offset):
        total = db.scalar(select(func.count()).select_from(statement.order_by(None).subquery()))
        return {"items": [record(item) for item in db.scalars(statement.limit(limit).offset(offset))], "total": total, "limit": limit, "offset": offset}

    @router.get("/overview")
    def overview(db=Depends(database)):
        counts = {name: db.scalar(select(func.count()).select_from(model)) for name, model in {"restaurants": d.Restaurant, "employees": d.Employee, "visits": d.Visit, "assets": d.Asset, "evidence": d.Evidence, "controls": d.Control}.items()}
        latest = db.scalar(select(Assessment).where(Assessment.status == "completed").order_by(Assessment.created_at.desc(), text("assessments.rowid DESC")).limit(1))
        assessed = db.scalar(select(func.count(func.distinct(AssessmentResult.restaurant_id))).join(Assessment).where(Assessment.status == "completed"))
        return {**counts, "assessment_status": "assessed" if assessed else "not_assessed", "assessed_restaurants": assessed, "latest_assessment": assessment_dict(latest) if latest else None, "open_findings": db.scalar(select(func.count()).select_from(Finding).where(Finding.status != "resolved")), "resolved_findings": db.scalar(select(func.count()).select_from(Finding).where(Finding.status == "resolved")), "months": list(db.scalars(select(d.EmployeePayroll.payroll_month).distinct().order_by(d.EmployeePayroll.payroll_month.desc()))), "role": "General Manager", "latest_import": db.scalar(select(func.max(d.CollectionRun.imported_at)))}

    @router.get("/restaurants")
    def restaurants(db=Depends(database)):
        items = []
        for item in db.scalars(select(d.Restaurant).order_by(d.Restaurant.id)):
            result = latest_result(db, item.id)
            items.append({**record(item), "employee_count": db.scalar(select(func.count()).select_from(d.RestaurantAssignment).where(d.RestaurantAssignment.restaurant_id == item.id)), "visit_count": db.scalar(select(func.count()).select_from(d.Visit).where(d.Visit.restaurant_id == item.id)), "asset_count": db.scalar(select(func.count()).select_from(d.Asset).where(d.Asset.restaurant_id == item.id)), "assessment_status": "assessed" if result else "not_assessed", "latest_assessment_id": result.assessment_id if result else None})
        return {"items": items, "total": len(items)}

    @router.get("/restaurants/{restaurant_id}")
    def restaurant_detail(restaurant_id: str, db=Depends(database)):
        ensure_restaurant(db, restaurant_id)
        result = latest_result(db, restaurant_id)
        return {**record(db.get(d.Restaurant, restaurant_id)), "assessment_status": "assessed" if result else "not_assessed", "latest_assessment_id": result.assessment_id if result else None, "assets": [record(a) for a in db.scalars(select(d.Asset).where(d.Asset.restaurant_id == restaurant_id))], "employee_ids": list(db.scalars(select(d.RestaurantAssignment.employee_id).where(d.RestaurantAssignment.restaurant_id == restaurant_id))), "visits": db.scalar(select(func.count()).select_from(d.Visit).where(d.Visit.restaurant_id == restaurant_id))}

    @router.get("/employees")
    def employees(restaurant_id: str | None = None, role: str | None = Query(None, pattern=r"^(General Manager|Manager|Staff)$"), limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0), db=Depends(database)):
        ensure_restaurant(db, restaurant_id)
        query = select(d.Employee).order_by(d.Employee.id)
        if restaurant_id:
            query = query.where(d.Employee.id.in_(select(d.RestaurantAssignment.employee_id).where(d.RestaurantAssignment.restaurant_id == restaurant_id)))
        if role:
            query = query.where(d.Employee.job_role == role)
        result = page(db, query, limit, offset)
        for item in result["items"]:
            item["restaurant_ids"] = list(db.scalars(select(d.RestaurantAssignment.restaurant_id).where(d.RestaurantAssignment.employee_id == item["id"]).order_by(d.RestaurantAssignment.restaurant_id)))
            item["platform_access"] = item["job_role"] == "General Manager"
        return result

    @router.get("/employees/{employee_id}")
    def employee_detail(employee_id: str, db=Depends(database)):
        item = db.get(d.Employee, employee_id)
        if item is None:
            raise HTTPException(404, "Employee not found")
        return {**record(item), "platform_access": item.job_role == "General Manager", "assignments": [record(r) for r in db.scalars(select(d.RestaurantAssignment).where(d.RestaurantAssignment.employee_id == employee_id))], "payroll": [record(r) for r in db.scalars(select(d.EmployeePayroll).where(d.EmployeePayroll.employee_id == employee_id).order_by(d.EmployeePayroll.payroll_month.desc()))], "allocations": [record(r) for r in db.scalars(select(d.PayrollAllocation).where(d.PayrollAllocation.employee_id == employee_id))]}

    @router.get("/visits")
    def visits(restaurant_id: str | None = None, month: str | None = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), member: bool | None = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), db=Depends(database)):
        ensure_restaurant(db, restaurant_id)
        query = select(d.Visit).order_by(d.Visit.visited_at.desc(), d.Visit.id)
        if restaurant_id:
            query = query.where(d.Visit.restaurant_id == restaurant_id)
        if month:
            query = query.where(d.Visit.visited_at.startswith(month))
        if member is not None:
            query = query.where(d.Visit.loyalty_member_at_visit == member)
        return page(db, query, limit, offset)

    @router.get("/visits/days")
    def visit_days(restaurant_id: str | None = None, month: str | None = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), member: bool | None = None, limit: int = Query(25, ge=1, le=200), offset: int = Query(0, ge=0), db=Depends(database)):
        ensure_restaurant(db, restaurant_id)
        query = select(d.Visit).order_by(d.Visit.visited_at, d.Visit.id)
        if restaurant_id:
            query = query.where(d.Visit.restaurant_id == restaurant_id)
        if month:
            query = query.where(d.Visit.visited_at.startswith(month))
        if member is not None:
            query = query.where(d.Visit.loyalty_member_at_visit == member)
        grouped = {}
        for visit in db.scalars(query):
            day = visit.visited_at[:10]
            key = visit.restaurant_id, day
            item = grouped.setdefault(key, {"id": f"V-{visit.restaurant_id}-{day}", "restaurant_id": visit.restaurant_id, "visit_date": day, "visit_count": 0, "member_visits": 0, "non_member_visits": 0, "payments": {}, "evidence_id": visit.evidence_id})
            item["visit_count"] += 1
            item["member_visits" if visit.loyalty_member_at_visit else "non_member_visits"] += 1
            if visit.currency is not None and visit.payment_received_minor is not None:
                item["payments"][visit.currency] = item["payments"].get(visit.currency, 0) + visit.payment_received_minor
        rows = sorted(grouped.values(), key=lambda row: (row["visit_date"], row["restaurant_id"]))
        return {"items": rows[offset:offset + limit], "total": len(rows), "visits_total": sum(r["visit_count"] for r in rows), "limit": limit, "offset": offset}

    @router.get("/resource-planning/summary")
    def summary(month: str | None = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), db=Depends(database)):
        return planning_summary(db, settings, month)

    @router.get("/data/{dataset}")
    def data(dataset: str, restaurant_id: str | None = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), db=Depends(database)):
        model = d.DATASETS.get(dataset)
        if model is None:
            raise HTTPException(404, "Dataset not found")
        ensure_restaurant(db, restaurant_id)
        query = select(model).order_by(*inspect(model).primary_key)
        if restaurant_id:
            if dataset == "restaurants":
                query = query.where(model.id == restaurant_id)
            elif dataset == "employees":
                query = query.where(model.id.in_(select(d.RestaurantAssignment.employee_id).where(d.RestaurantAssignment.restaurant_id == restaurant_id)))
            elif hasattr(model, "restaurant_id"):
                query = query.where(model.restaurant_id == restaurant_id)
            elif hasattr(model, "asset_id"):
                query = query.where(model.asset_id.in_(select(d.Asset.id).where(d.Asset.restaurant_id == restaurant_id)))
            elif hasattr(model, "employee_id"):
                query = query.where(model.employee_id.in_(select(d.RestaurantAssignment.employee_id).where(d.RestaurantAssignment.restaurant_id == restaurant_id)))
            elif dataset == "consent_events":
                query = query.where(model.customer_id.in_(select(d.LoyaltyRecord.customer_id).where(d.LoyaltyRecord.restaurant_id == restaurant_id)))
            elif dataset == "customers":
                customers = select(d.Visit.customer_id).where(d.Visit.restaurant_id == restaurant_id).union(select(d.LoyaltyRecord.customer_id).where(d.LoyaltyRecord.restaurant_id == restaurant_id))
                query = query.where(model.id.in_(customers))
            elif dataset == "processing_purposes":
                query = query.where(model.id.in_(select(d.LoyaltyRecord.purpose_id).where(d.LoyaltyRecord.restaurant_id == restaurant_id)))
        return page(db, query, limit, offset)

    @router.get("/controls")
    def controls(db=Depends(database)):
        items = []
        for control in db.scalars(select(d.Control).order_by(d.Control.id)):
            results = [latest_result(db, rid, control.id) for rid in db.scalars(select(d.Restaurant.id))]
            assessed = [r for r in results if r]
            status = next((s for s in ["fail", "error", "unknown", "pass", "not_applicable"] if any(r.status == s for r in assessed)), "not_assessed")
            items.append({**record(control), "status": status, "assessed_restaurants": len(assessed), "mappings": [record(m) for m in db.scalars(select(d.ControlMapping).where(d.ControlMapping.control_id == control.id))]})
        return {"items": items, "total": len(items), "frameworks": [record(f) for f in db.scalars(select(d.Framework).order_by(d.Framework.id))]}

    @router.get("/collections")
    def collections(db=Depends(database)):
        return {"items": [record(r) for r in db.scalars(select(d.CollectionRun).order_by(d.CollectionRun.imported_at.desc()))]}

    @router.get("/evidence")
    def evidence(restaurant_id: str | None = None, dataset: str | None = None, month: str | None = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), db=Depends(database)):
        ensure_restaurant(db, restaurant_id)
        query = select(d.Evidence).order_by(d.Evidence.captured_at.desc(), d.Evidence.id)
        if dataset:
            query = query.where(d.Evidence.dataset_type == dataset)
        if month:
            query = query.where(d.Evidence.period_month == month)
        rows = [record(r) for r in db.scalars(query) if not restaurant_id or restaurant_id in r.restaurant_ids]
        return {"items": rows[offset:offset + limit], "total": len(rows), "limit": limit, "offset": offset}

    @router.get("/evidence/{evidence_id}")
    def evidence_detail(evidence_id: str, db=Depends(database)):
        item = db.get(d.Evidence, evidence_id)
        if item is None:
            raise HTTPException(404, "Evidence not found")
        return {**record(item), "collection": record(db.get(d.CollectionRun, item.collection_run_id)), "control_ids": list(db.scalars(select(d.ControlEvidence.control_id).where(d.ControlEvidence.evidence_id == item.id)))}

    @router.get("/evidence/{evidence_id}/content")
    def evidence_content(evidence_id: str, download: bool = False, db=Depends(database)):
        item = db.get(d.Evidence, evidence_id)
        if item is None:
            raise HTTPException(404, "Evidence not found")
        payload, path = evidence_payload(settings, item)
        if download:
            return FileResponse(path, media_type="application/json", filename=f"{item.dataset_type}.json")
        return payload

    @router.get("/evidence/{evidence_id}/annotations")
    def evidence_annotations(evidence_id: str, result_id: str | None = None, assessment_id: str | None = None,
                             restaurant_id: str | None = None, framework_id: str | None = None,
                             severity: str | None = None, db=Depends(database)):
        from app.evidence_annotations import annotations
        return annotations(db, settings, evidence_id, result_id, assessment_id, restaurant_id, framework_id, severity)

    return router
