"""Imported demo records. These are integration views, not an operational ERP."""
from sqlalchemy import JSON, CheckConstraint, ForeignKey, ForeignKeyConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class CollectionRun(Base):
    __tablename__ = "collection_runs"
    id: Mapped[str] = mapped_column(primary_key=True)
    source_type: Mapped[str]
    source_version: Mapped[str]
    source_hash: Mapped[str]
    captured_at: Mapped[str]
    imported_at: Mapped[str]
    status: Mapped[str]
    restaurant_ids: Mapped[list] = mapped_column(JSON)


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[str] = mapped_column(primary_key=True)
    collection_run_id: Mapped[str] = mapped_column(ForeignKey("collection_runs.id"), index=True)
    dataset_type: Mapped[str] = mapped_column(index=True)
    captured_at: Mapped[str]
    restaurant_ids: Mapped[list] = mapped_column(JSON)
    asset_ids: Mapped[list] = mapped_column(JSON)
    relative_path: Mapped[str] = mapped_column(unique=True)
    sha256: Mapped[str]
    record_count: Mapped[int]
    complete: Mapped[bool]
    period_month: Mapped[str | None] = mapped_column(String(7))


class SourceRecord:
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(ForeignKey("evidence.id"), index=True)


class Restaurant(SourceRecord, Base):
    __tablename__ = "restaurants"
    active: Mapped[bool]


class Employee(SourceRecord, Base):
    __tablename__ = "employees"
    __table_args__ = (CheckConstraint("job_role IN ('General Manager','Manager','Staff')", name="ck_employee_role"),)
    name: Mapped[str]
    job_role: Mapped[str]
    active: Mapped[bool]


class RestaurantAssignment(Base):
    __tablename__ = "restaurant_assignments"
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), primary_key=True)
    restaurant_id: Mapped[str] = mapped_column(ForeignKey("restaurants.id"), primary_key=True, index=True)
    evidence_id: Mapped[str] = mapped_column(ForeignKey("evidence.id"))


class Asset(SourceRecord, Base):
    __tablename__ = "assets"
    restaurant_id: Mapped[str | None] = mapped_column(ForeignKey("restaurants.id"), index=True)
    owner_employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"))
    asset_type: Mapped[str]
    name: Mapped[str]
    active: Mapped[bool]


class EmployeePayroll(Base):
    __tablename__ = "employee_payroll"
    __table_args__ = (CheckConstraint("monthly_salary_minor >= 0 AND allowance_minor >= 0", name="ck_payroll_nonnegative"),)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), primary_key=True)
    payroll_month: Mapped[str] = mapped_column(String(7), primary_key=True)
    currency: Mapped[str] = mapped_column(String(3))
    monthly_salary_minor: Mapped[int]
    allowance_minor: Mapped[int]
    evidence_id: Mapped[str] = mapped_column(ForeignKey("evidence.id"))


class PayrollAllocation(Base):
    __tablename__ = "payroll_allocations"
    __table_args__ = (
        ForeignKeyConstraint(["employee_id", "payroll_month"], ["employee_payroll.employee_id", "employee_payroll.payroll_month"]),
        CheckConstraint("salary_allocated_minor >= 0 AND allowance_allocated_minor >= 0", name="ck_allocation_nonnegative"),
    )
    employee_id: Mapped[str] = mapped_column(primary_key=True)
    payroll_month: Mapped[str] = mapped_column(String(7), primary_key=True)
    restaurant_id: Mapped[str] = mapped_column(ForeignKey("restaurants.id"), primary_key=True, index=True)
    salary_allocated_minor: Mapped[int]
    allowance_allocated_minor: Mapped[int]
    evidence_id: Mapped[str] = mapped_column(ForeignKey("evidence.id"))


class SalesTransaction(SourceRecord, Base):
    __tablename__ = "sales_transactions"
    __table_args__ = (
        CheckConstraint("amount_minor >= 0", name="ck_sale_nonnegative"),
        CheckConstraint("transaction_kind IN ('sale','refund') AND status IN ('posted','void')", name="ck_sale_kind_status"),
    )
    restaurant_id: Mapped[str] = mapped_column(ForeignKey("restaurants.id"), index=True)
    transaction_at: Mapped[str] = mapped_column(index=True)
    transaction_kind: Mapped[str]
    status: Mapped[str]
    amount_minor: Mapped[int]
    currency: Mapped[str] = mapped_column(String(3))
    reference_sale_id: Mapped[str | None] = mapped_column(ForeignKey("sales_transactions.id"))


class AccessApproval(SourceRecord, Base):
    __tablename__ = "erp_access_approvals"
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"), index=True)
    permission: Mapped[str]
    status: Mapped[str]
    valid_from: Mapped[str]
    valid_until: Mapped[str | None]


class AccessGrant(SourceRecord, Base):
    __tablename__ = "actual_access_grants"
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"), index=True)
    permission: Mapped[str]
    active: Mapped[bool]


class Customer(SourceRecord, Base):
    __tablename__ = "customers"
    display_name: Mapped[str]
    loyalty_member: Mapped[bool]


class ProcessingPurpose(SourceRecord, Base):
    __tablename__ = "processing_purposes"
    name: Mapped[str]
    lawful_basis: Mapped[str]
    retention_days: Mapped[int]


class LoyaltyRecord(SourceRecord, Base):
    __tablename__ = "loyalty_records"
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    restaurant_id: Mapped[str] = mapped_column(ForeignKey("restaurants.id"), index=True)
    purpose_id: Mapped[str] = mapped_column(ForeignKey("processing_purposes.id"))
    collected_at: Mapped[str]
    retention_due_at: Mapped[str]
    status: Mapped[str]


class ConsentEvent(SourceRecord, Base):
    __tablename__ = "consent_events"
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    purpose_id: Mapped[str] = mapped_column(ForeignKey("processing_purposes.id"))
    event_type: Mapped[str]
    occurred_at: Mapped[str]
    notice_version: Mapped[str | None]


class Visit(SourceRecord, Base):
    __tablename__ = "visits"
    restaurant_id: Mapped[str] = mapped_column(ForeignKey("restaurants.id"), index=True)
    customer_id: Mapped[str | None] = mapped_column(ForeignKey("customers.id"))
    visited_at: Mapped[str] = mapped_column(index=True)
    loyalty_member_at_visit: Mapped[bool]
    # Nullable only for migrations of legacy snapshots; new source contracts require all three.
    payment_received_minor: Mapped[int | None]
    currency: Mapped[str | None] = mapped_column(String(3))
    sales_transaction_id: Mapped[str | None] = mapped_column(ForeignKey("sales_transactions.id"), unique=True, index=True)


class CustomerActivity(SourceRecord, Base):
    __tablename__ = "customer_activities"
    loyalty_record_id: Mapped[str] = mapped_column(ForeignKey("loyalty_records.id"))
    visit_id: Mapped[str | None] = mapped_column(ForeignKey("visits.id"))
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"), index=True)
    activity_type: Mapped[str]
    occurred_at: Mapped[str]


class CustomerRequest(SourceRecord, Base):
    __tablename__ = "customer_requests"
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"))
    restaurant_id: Mapped[str] = mapped_column(ForeignKey("restaurants.id"), index=True)
    request_type: Mapped[str]
    received_at: Mapped[str]
    due_at: Mapped[str]
    completed_at: Mapped[str | None]
    status: Mapped[str]


class EmployeeAccessEvent(SourceRecord, Base):
    __tablename__ = "employee_access_events"
    __table_args__ = (CheckConstraint("outcome IN ('succeeded','denied')", name="ck_access_outcome"),)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), index=True)
    restaurant_id: Mapped[str] = mapped_column(ForeignKey("restaurants.id"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"))
    target_customer_id: Mapped[str | None] = mapped_column(ForeignKey("customers.id"))
    action: Mapped[str]
    occurred_at: Mapped[str] = mapped_column(index=True)
    outcome: Mapped[str]


class PaymentConfiguration(Base):
    __tablename__ = "payment_configurations"
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"), primary_key=True)
    settings_json: Mapped[dict] = mapped_column(JSON)
    policy_json: Mapped[dict] = mapped_column(JSON)
    evidence_id: Mapped[str] = mapped_column(ForeignKey("evidence.id"))


class Framework(Base):
    __tablename__ = "frameworks"
    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    version: Mapped[str]
    source_url: Mapped[str]


class Control(Base):
    __tablename__ = "controls"
    id: Mapped[str] = mapped_column(primary_key=True)
    title: Mapped[str]
    rule_version: Mapped[str]
    target_type: Mapped[str]
    severity: Mapped[str]
    parameters_json: Mapped[dict] = mapped_column(JSON)


class ControlMapping(Base):
    __tablename__ = "control_mappings"
    control_id: Mapped[str] = mapped_column(ForeignKey("controls.id"), primary_key=True)
    framework_id: Mapped[str] = mapped_column(ForeignKey("frameworks.id"), primary_key=True)
    requirement_ref: Mapped[str] = mapped_column(primary_key=True)
    mapping_note: Mapped[str]


class ControlEvidence(Base):
    __tablename__ = "control_evidence"
    control_id: Mapped[str] = mapped_column(ForeignKey("controls.id"), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(ForeignKey("evidence.id"), primary_key=True)


# Dependency order is explicit: the connector flushes each dataset before its dependants.
DATASETS = {
    "restaurants": Restaurant, "employees": Employee,
    "restaurant_assignments": RestaurantAssignment, "assets": Asset,
    "employee_payroll": EmployeePayroll, "payroll_allocations": PayrollAllocation,
    "sales_transactions": SalesTransaction, "erp_access_approvals": AccessApproval,
    "customers": Customer, "processing_purposes": ProcessingPurpose,
    "loyalty_records": LoyaltyRecord, "consent_events": ConsentEvent, "visits": Visit,
    "customer_activities": CustomerActivity, "customer_requests": CustomerRequest,
    "actual_access_grants": AccessGrant, "employee_access_events": EmployeeAccessEvent,
    "payment_configurations": PaymentConfiguration,
}
