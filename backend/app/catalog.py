"""Demo-support mappings; these checks do not cover entire regulatory requirements."""
from sqlalchemy import delete, select

from app.domain import Control, ControlEvidence, ControlMapping, Evidence, Framework

RULES = [
    ("LOY-01", "Consent evidence", "GDPR", "Articles 4(11), 6(1)(a) and 7(1)", ["loyalty_records", "processing_purposes", "consent_events", "customer_activities"], "high"),
    ("LOY-02", "Consent withdrawal", "GDPR", "Article 7(3)", ["loyalty_records", "processing_purposes", "consent_events", "customer_activities"], "high"),
    ("LOY-03", "Retention", "GDPR", "Article 5(1)(e)", ["loyalty_records", "processing_purposes"], "medium"),
    ("LOY-04", "Customer requests", "GDPR", "Article 12(3)", ["customer_requests"], "medium"),
    ("LOY-05", "Authorized access and actions", "GDPR", "Articles 5(1)(f), 32(1)(b) and 32(4)", ["erp_access_approvals", "actual_access_grants", "employee_access_events", "employees", "restaurant_assignments"], "high"),
    ("PAY-01", "Secure transmission", "PCI-DSS", "Requirement 4.2.1", ["payment_configurations"], "high"),
    ("PAY-02", "Authentication", "PCI-DSS", "Requirements 8.3.6 and 8.4.2", ["payment_configurations"], "high"),
    ("PAY-03", "Audit logging", "PCI-DSS", "Requirement 10.2.1", ["payment_configurations"], "high"),
    ("PAY-04", "Patch policy", "PCI-DSS", "Requirement 6.3.3", ["payment_configurations"], "medium"),
    ("PAY-05", "Prohibited storage", "PCI-DSS", "Requirement 3.3.1", ["payment_configurations"], "critical"),
]

# IDs and the six-field RULES tuples are stable interfaces for the evaluator.
# Inventory coverage/freshness for assets and restaurants is checked globally.
RULE_VERSION = "1.0.0"
MAPPING_NOTES = {
    "LOY-01": (
        "Checks affirmative consent evidence, purpose, capture time and notice version only "
        "for activities configured to rely on consent. Does not establish that consent was "
        "freely given, specific, informed or otherwise legally valid, or assess other lawful bases."
    ),
    "LOY-02": (
        "Checks consent-based marketing after withdrawal against later valid re-consent. "
        "Does not assess withdrawal accessibility, prior notices, erasure obligations or "
        "direct-marketing objections under Article 21."
    ),
    "LOY-03": (
        "Checks active records against configured purpose-specific retention deadlines. "
        "GDPR sets no universal loyalty retention period; necessity, legal retention duties, "
        "exceptions and deletion effectiveness require separate review."
    ),
    "LOY-04": (
        "Tracks unresolved requests against recorded due dates, supporting response timing "
        "for rights under Articles 15 to 22. Article 12(3) requires action information without "
        "undue delay and within one month, with a justified two-month extension and notice "
        "within the first month. Does not validate due-date legality, extensions, response "
        "delivery/content or fulfillment of access/erasure rights."
    ),
    "LOY-05": (
        "Compares actual grants and successful employee actions with ERP-approved permissions "
        "and employee/restaurant scope at the relevant time. Approvals are demo evidence of "
        "authorized instructions; this does not establish lawful instructions or assess all "
        "risk-appropriate security measures, confidentiality, availability or resilience."
    ),
    "PAY-01": (
        "Checks configured TLS enablement/version for demo endpoints assumed to transmit PAN "
        "over open, public networks. The TLS allowlist is demo policy; certificate trust, "
        "validity/revocation, cipher strength, downgrade protection and actual transmissions "
        "are not verified."
    ),
    "PAY-02": (
        "Checks configured password length and MFA flags for scoped demo accounts. Supports "
        "the length portion of 8.3.6 and MFA for non-console CDE access under 8.4.2 only. "
        "Does not verify password composition, effective MFA enforcement, factor independence "
        "or requirement-specific applicability/exceptions; configured policy is not a full "
        "Requirement 8 assessment."
    ),
    "PAY-03": (
        "Checks the audit-logging-enabled configuration flag only. Does not prove logging "
        "is active, captures all required events/fields, or meets log protection, review "
        "and retention requirements."
    ),
    "PAY-04": (
        "Compares installed and required patch versions under demo policy only. Requirement "
        "6.3.3 requires critical-vulnerability patches within one month of release and other "
        "applicable patches within risk-based time frames. Version matching does not validate "
        "release/install dates, risk rankings, patch completeness or those deadlines."
    ),
    "PAY-05": (
        "Uses boolean metadata as a proxy for prohibited sensitive authentication data "
        "storage after authorization (full track data, card verification codes, PIN/PIN blocks), "
        "even if encrypted, in the demo merchant scope. Does not inspect actual data, verify "
        "secure deletion or assess all stored PAN protections; PAN storage is not universally "
        "prohibited. No sensitive payment values are collected by this check."
    ),
}


def seed_catalog(db):
    for id, name, version, url in [
        ("GDPR", "GDPR", "Regulation (EU) 2016/679", "https://eur-lex.europa.eu/eli/reg/2016/679/oj/eng"),
        ("PCI-DSS", "PCI DSS", "4.0.1", "https://www.pcisecuritystandards.org/document_library/?class=pcidss&doc=pci_dss"),
    ]:
        db.merge(Framework(id=id, name=name, version=version, source_url=url))
    db.flush()
    for id, title, framework, reference, datasets, severity in RULES:
        db.merge(Control(id=id, title=title, rule_version=RULE_VERSION, target_type="restaurant", severity=severity, parameters_json={"datasets": datasets, "evaluation_implemented": True}))
        db.flush()
        # requirement_ref is part of the primary key: merge alone retains old mappings.
        db.execute(delete(ControlMapping).where(ControlMapping.control_id == id))
        db.merge(ControlMapping(control_id=id, framework_id=framework, requirement_ref=reference, mapping_note="Supporting demo check only; not a full regulatory assessment or certification. " + MAPPING_NOTES[id]))
        for evidence in db.scalars(select(Evidence).where(Evidence.dataset_type.in_(datasets))):
            db.merge(ControlEvidence(control_id=id, evidence_id=evidence.id))
    db.flush()
