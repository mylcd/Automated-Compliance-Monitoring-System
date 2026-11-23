"""Strict contracts for the three local source connectors."""
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter


def utc(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Source timestamps must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


Timestamp = Annotated[str, AfterValidator(utc)]
Month = Annotated[str, Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
Amount = Annotated[int, Field(ge=0)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_min_length=1)


class RestaurantInput(Record):
    id: str
    active: bool


class EmployeeInput(Record):
    id: str
    name: str
    job_role: Literal["General Manager", "Manager", "Staff"]
    active: bool


class AssignmentInput(Record):
    employee_id: str
    restaurant_id: str


class AssetInput(Record):
    id: str
    restaurant_id: str | None
    owner_employee_id: str
    asset_type: Literal["loyalty", "payment", "erp"]
    name: str
    active: bool


class PayrollInput(Record):
    employee_id: str
    payroll_month: Month
    currency: Currency
    monthly_salary_minor: Amount
    allowance_minor: Amount


class AllocationInput(Record):
    employee_id: str
    payroll_month: Month
    restaurant_id: str
    salary_allocated_minor: Amount
    allowance_allocated_minor: Amount


class SaleInput(Record):
    id: str
    restaurant_id: str
    transaction_at: Timestamp
    transaction_kind: Literal["sale", "refund"]
    status: Literal["posted", "void"]
    amount_minor: Amount
    currency: Currency
    reference_sale_id: str | None


class ApprovalInput(Record):
    id: str
    employee_id: str
    asset_id: str
    permission: Literal["customer.read", "customer.update", "customer.delete"]
    status: Literal["approved", "rejected"]
    valid_from: Timestamp
    valid_until: Timestamp | None


class GrantInput(Record):
    id: str
    employee_id: str
    asset_id: str
    permission: Literal["customer.read", "customer.update", "customer.delete"]
    active: bool


class CustomerInput(Record):
    id: str
    display_name: str
    loyalty_member: bool


class PurposeInput(Record):
    id: str
    name: str
    lawful_basis: Literal["consent", "contract", "legitimate_interest"]
    retention_days: Annotated[int, Field(gt=0)]


class LoyaltyInput(Record):
    id: str
    customer_id: str
    restaurant_id: str
    purpose_id: str
    collected_at: Timestamp
    retention_due_at: Timestamp
    status: Literal["active", "deleted"]


class ConsentInput(Record):
    id: str
    customer_id: str
    purpose_id: str
    event_type: Literal["grant", "withdraw"]
    occurred_at: Timestamp
    notice_version: str | None


class VisitInput(Record):
    id: str
    restaurant_id: str
    customer_id: str | None
    visited_at: Timestamp
    loyalty_member_at_visit: bool
    payment_received_minor: Amount
    currency: Currency
    sales_transaction_id: str


class VisitSeed(Record):
    schema_version: Literal["1.0"]
    restaurant_id: str
    month: Month
    currency: Currency
    captured_at: Timestamp
    records: list[VisitInput]


class ActivityInput(Record):
    id: str
    loyalty_record_id: str
    visit_id: str | None
    asset_id: str
    activity_type: Literal["marketing.email", "loyalty.reward"]
    occurred_at: Timestamp


class RequestInput(Record):
    id: str
    customer_id: str
    restaurant_id: str
    request_type: Literal["access", "deletion"]
    received_at: Timestamp
    due_at: Timestamp
    completed_at: Timestamp | None
    status: Literal["open", "completed"]


class AccessEventInput(Record):
    id: str
    employee_id: str
    restaurant_id: str
    asset_id: str
    target_customer_id: str | None
    action: Literal["customer.read", "customer.update", "customer.delete"]
    occurred_at: Timestamp
    outcome: Literal["succeeded", "denied"]


class PaymentAccount(Record):
    id: str
    active: bool
    mfa_enabled: bool
    password_length: Annotated[int, Field(ge=0, le=1024)]


class PaymentSettings(Record):
    tls_enabled: bool
    tls_version: str
    audit_logging_enabled: bool
    installed_patch_version: str
    prohibited_storage_detected: bool
    accounts: list[PaymentAccount]


class PaymentPolicy(Record):
    allowed_tls_versions: Annotated[list[str], Field(min_length=1)]
    require_mfa: bool
    minimum_password_length: Annotated[int, Field(ge=1)]
    required_patch_version: str


class PaymentInput(Record):
    asset_id: str
    settings_json: PaymentSettings
    policy_json: PaymentPolicy


CONTRACTS = dict(zip(
    ["restaurants", "employees", "restaurant_assignments", "assets", "employee_payroll",
     "payroll_allocations", "sales_transactions", "erp_access_approvals", "customers",
     "processing_purposes", "loyalty_records", "consent_events", "visits",
     "customer_activities", "customer_requests", "actual_access_grants",
     "employee_access_events", "payment_configurations"],
    [RestaurantInput, EmployeeInput, AssignmentInput, AssetInput, PayrollInput,
     AllocationInput, SaleInput, ApprovalInput, CustomerInput, PurposeInput,
     LoyaltyInput, ConsentInput, VisitInput, ActivityInput, RequestInput,
     GrantInput, AccessEventInput, PaymentInput], strict=True,
))


class SourceBundle(Record):
    schema_version: Literal["1.0"]
    source_type: Literal["erp", "loyalty", "payment"]
    source_version: str
    captured_at: Timestamp
    restaurant_ids: list[str]
    complete: bool
    complete_months: list[Month]
    datasets: dict[str, list[dict]]


def validate_rows(name: str, rows: list[dict]) -> list[dict]:
    return [row.model_dump() for row in TypeAdapter(list[CONTRACTS[name]]).validate_python(rows, strict=True)]
