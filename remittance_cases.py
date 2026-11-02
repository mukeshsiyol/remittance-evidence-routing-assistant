"""Synthetic cases, labels, adjudication checks, and simple baseline rules."""
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional

CLOSED_LABELS = {"royalty", "fees for technical services", "business income", "reimbursement", "needs review"}

PAYMENT_NATURES = {"royalty", "fees for technical services", "business income", "reimbursement", "uncertain"}

NR = "needs review"

ERR = "api_error"

@dataclass
class RemittanceCase:
    case_id: int
    name: str
    credit_date: str                              # YYYY-MM-DD
    payment_date: str                             # YYYY-MM-DD
    payee_country: str                            # "US", "UK", "SG", "NONE"
    payee_type: str                               # "COMPANY" or "INDIVIDUAL"
    amount_inr: float
    same_tax_year_payee_taxable_agg_inr: float    # earlier taxable remittances to this payee, same tax year
    has_trc: Optional[bool]                  # True=present, False=confirmed absent, None=unknown
    has_portal_treaty_form: Optional[bool]
    has_no_pe_declaration: Optional[bool]
    has_ao_sec395_certificate: Optional[bool]
    invoice_text: str
    contract_text: str
    gold_label: Optional[str]                     # legacy combined label; None until adjudicated
    gold_payment_nature: Optional[str] = None
    gold_review_required: Optional[bool] = None
    gold_adjudication_record: Optional[Dict] = None

    def __post_init__(self):
        if self.gold_label in CLOSED_LABELS and self.gold_payment_nature is None:
            self.gold_payment_nature = self.gold_label if self.gold_label != NR else "uncertain"
        if self.gold_label in CLOSED_LABELS and self.gold_review_required is None:
            self.gold_review_required = self.gold_label == NR

def case_is_adjudicated(case: RemittanceCase) -> bool:
    return (
        case.gold_payment_nature in PAYMENT_NATURES
        and isinstance(case.gold_review_required, bool)
    )

def case_has_independent_blind_review(case: RemittanceCase) -> bool:
    """Require reviewer provenance before holdout results can be scored."""
    record = case.gold_adjudication_record
    if not case_is_adjudicated(case) or not isinstance(record, dict):
        return False
    required_text = ("reviewer_name", "reviewer_role", "review_date", "rationale")
    if not all(isinstance(record.get(key), str) and record[key].strip() for key in required_text):
        return False
    if record.get("reviewer_qualified") is not True:
        return False
    if record.get("blind_to_model_outputs") is not True:
        return False
    source_refs = record.get("source_references")
    if not isinstance(source_refs, list) or not source_refs or not all(
        isinstance(item, str) and item.strip() for item in source_refs
    ):
        return False
    try:
        date.fromisoformat(record["review_date"])
    except ValueError:
        return False
    return True

def gold_routing_label(case: RemittanceCase) -> str:
    """Combine the independent gold axes for backward-compatible final routing."""
    if case.gold_review_required or case.gold_payment_nature == "uncertain":
        return NR
    return case.gold_payment_nature or "uncertain"

def evidence_status(value: Optional[bool]) -> str:
    return "supplied" if value is True else ("missing" if value is False else "unknown")

def case_evidence_inventory(case: RemittanceCase) -> Dict[str, Dict[str, str]]:
    """Expose each document assertion and its fixture-level provenance to reviewers and the model."""
    fields = {
        "tax_residency_certificate": ("has_trc", case.has_trc),
        "portal_treaty_form": ("has_portal_treaty_form", case.has_portal_treaty_form),
        "no_pe_declaration": ("has_no_pe_declaration", case.has_no_pe_declaration),
        "section_395_certificate": ("has_ao_sec395_certificate", case.has_ao_sec395_certificate),
    }
    inventory = {}
    for document, (field_name, value) in fields.items():
        status = evidence_status(value)
        source = f"synthetic case fixture metadata: {field_name}={value!r}"
        if status == "unknown":
            detail = "The case narrative and fixture do not specify the status."
        else:
            detail = "Fixture assertion only; the supporting document or independent absence check is not attached."
        inventory[document] = {"status": status, "source": source, "source_detail": detail}
    return inventory

TEST_CASES: List[RemittanceCase] = [
    RemittanceCase(
        1, "Industrial Patent & Secret Chemical Process Licence (US Corp)",
        "2026-05-10", "2026-05-15", "US", "COMPANY", 900_000, 0, True, True, True, False,
        "[SYNTHETIC INVOICE #US-01]: Annual license fee for use of US Patent #9,882,110 and secret thermal polymerization process in client's Gujarat plant - INR 9,00,000.",
        "[SYNTHETIC CONTRACT CLAUSE 2.1]: Licensor grants Licensee a non-exclusive license to use the patented chemical process and secret formula in manufacturing. No copyright in software or literary work is involved.",
        "royalty"),
    RemittanceCase(
        2, "Scientific Lab Spectrometer Equipment Lease (UK Corp)",
        "2026-05-20", "2026-05-25", "UK", "COMPANY", 750_000, 0, True, True, True, False,
        "[SYNTHETIC INVOICE #UK-02]: Quarterly rental for right to use high-precision industrial mass spectrometer equipment shipped from London - INR 7,50,000.",
        "[SYNTHETIC CONTRACT CLAUSE 4.1]: Lessor leases the industrial and scientific testing equipment to Lessee for physical use at Lessee's Bengaluru facility.",
        "royalty"),
    RemittanceCase(
        3, "Contested Cloud SaaS CRM Subscription (US Corp)",
        "2026-06-01", "2026-06-05", "US", "COMPANY", 850_000, 0, True, True, True, False,
        "[SYNTHETIC INVOICE #US-03]: Annual enterprise subscription for 100 cloud software seats (hosted CRM platform) - INR 8,50,000.",
        "[SYNTHETIC CONTRACT CLAUSE 1.2]: Customer is granted right to access the hosted computer software application via browser; no source code or underlying copyright is transferred.",
        "needs review"),
    RemittanceCase(
        4, "SaaS Subscription Bundled with 40 Hours of Custom API Engineering (SG Corp)",
        "2026-06-12", "2026-06-18", "SG", "COMPANY", 1_400_000, 0, True, True, True, False,
        "[SYNTHETIC INVOICE #SG-04]: Enterprise Analytics Platform Annual Plan (Includes 40 hours of dedicated custom data-pipeline engineering and team training) - Single Fee INR 14,00,000.",
        "[SYNTHETIC CONTRACT CLAUSE 3.1]: For a consolidated annual fee of INR 14,00,000, Vendor shall provide platform access plus 40 hours of custom integration engineering and staff training.",
        "needs review"),
    RemittanceCase(
        5, "Verified Third-Party Airline Ticket Reimbursement (SG Corp)",
        "2026-07-01", "2026-07-04", "SG", "COMPANY", 110_000, 0, True, True, True, False,
        "[SYNTHETIC INVOICE #SG-05]: Exact cost pass-through reimbursement for Singapore Airlines Ticket #618-99201412 (Original airline tax invoice attached, zero markup) - INR 1,10,000.",
        "[SYNTHETIC CONTRACT CLAUSE 8.2]: Client shall reimburse Consultant for actual out-of-pocket airfare at exact cost without markup upon presentation of original carrier invoices.",
        "reimbursement", gold_review_required=True),
    RemittanceCase(
        6, "Contradictory Invoice vs Contract (Claimed Reimbursement on All-Inclusive Contract)",
        "2026-07-15", "2026-07-20", "UK", "COMPANY", 320_000, 400_000, True, True, True, False,
        "[SYNTHETIC INVOICE #UK-06]: Reimbursement of engineer travel, living, and administrative out-of-pocket expenses for Q2 - INR 3,20,000 (No third-party bills attached).",
        "[SYNTHETIC CONTRACT CLAUSE 5.1]: The consulting fee in Schedule A is strictly all-inclusive. No separate reimbursement for travel, living, or administrative expenses shall be payable by Client.",
        "needs review"),
    RemittanceCase(
        7, "Freelance Industrial Designer (UK Individual - Unknown India Stay Days)",
        "2026-08-01", "2026-08-05", "UK", "INDIVIDUAL", 620_000, 0, True, True, False, False,
        "[SYNTHETIC INVOICE #UK-IND-07]: Delivery of custom CAD industrial packaging blueprints and onsite/remote workshop - INR 6,20,000.",
        "[SYNTHETIC CONTRACT CLAUSE 2.0]: Individual Contractor shall deliver custom CAD blueprints and conduct workshops in Mumbai and London as needed (total days spent in India not recorded in contract).",
        "needs review"),
    RemittanceCase(
        8, "Standard Offshore Digital Banner Ad Placement (SG Corp)",
        "2026-08-10", "2026-08-14", "SG", "COMPANY", 480_000, 0, True, True, True, False,
        "[SYNTHETIC INVOICE #SG-08]: Automated banner advertisement impressions served on Southeast Asia travel portal for August campaign - INR 4,80,000.",
        "[SYNTHETIC CONTRACT CLAUSE 1.1]: Publisher places Advertiser's standard image banners on Publisher's foreign web portal; Publisher has no server, office, or employees in India and transfers no IP.",
        "business income", gold_review_required=True),
    # Revised fixture v2: the prior wording left the manuals' role ambiguous.
    # They are training aids with no separate fee, license, or independent exploitation right.
    RemittanceCase(
        9, "Calibration Training with Incidental Manuals (SG Corp)",
        "2026-09-01", "2026-09-06", "SG", "COMPANY", 680_000, 0, True, True, True, False,
        "[SYNTHETIC INVOICE #SG-09]: Intensive 5-day semiconductor laser calibration training; diagnostic checklists are incidental training aids included in the single fee of INR 6,80,000.",
        "[SYNTHETIC CONTRACT CLAUSE 3.4]: Vendor shall train Client's engineers. Calibration manuals and checklists are provided solely as incidental training aids, are not separately priced or licensed, and grant no independent right to exploit proprietary know-how.",
        "fees for technical services", gold_review_required=True),
    RemittanceCase(
        10, "Technical Security Audit from Non-Treaty Jurisdiction (NONE)",
        "2026-09-12", "2026-09-15", "NONE", "COMPANY", 720_000, 0, False, False, False, False,
        "[SYNTHETIC INVOICE #NT-10]: Remote penetration testing and technical vulnerability remediation report for Indian banking app - INR 7,20,000.",
        "[SYNTHETIC CONTRACT CLAUSE 1.0]: Consultant (incorporated in a non-treaty jurisdiction) shall perform technical security testing and deliver a remediation architecture report.",
        "fees for technical services"),
    RemittanceCase(
        11, "Non-Treaty Lead-Gen Vendor Claiming Business Income with Indian Agent",
        "2026-09-20", "2026-09-25", "NONE", "COMPANY", 950_000, 0, False, False, False, False,
        "[SYNTHETIC INVOICE #NT-11]: Commission for enterprise sales orders secured from Indian customers during Q2 - INR 9,50,000.",
        "[SYNTHETIC CONTRACT CLAUSE 2.2]: Vendor operates from a non-treaty jurisdiction and utilizes a dedicated representative in Mumbai to negotiate and conclude sales orders with Indian clients.",
        "needs review", gold_payment_nature="business income"),
    RemittanceCase(
        12, "Small Trademark Licence Below INR 5 Lakh Threshold (SG Corp)",
        "2026-10-01", "2026-10-04", "SG", "COMPANY", 220_000, 150_000, True, True, True, False,
        "[SYNTHETIC INVOICE #SG-12]: Quarterly fee for right to use registered ' EnviroSeal' trademark on Indian packaging - INR 2,20,000.",
        "[SYNTHETIC CONTRACT CLAUSE 6.1]: Licensor grants Licensee the right to display the registered trademark on goods sold in India.",
        "royalty"),
    RemittanceCase(
        13, "Straddling 31 March 2026 Transition (Credited Mar 24, Paid Apr 12)",
        "2026-03-24", "2026-04-12", "US", "COMPANY", 640_000, 0, True, True, True, False,
        "[SYNTHETIC INVOICE #US-13]: Patent licensing royalty credited to vendor account on 24 March 2026, remitted via AD Bank on 12 April 2026 - INR 6,40,000.",
        "[SYNTHETIC CONTRACT CLAUSE 2.1]: Licensee shall pay quarterly patent royalty for use of patented manufacturing valve design.",
        "needs review", gold_payment_nature="royalty"),
    RemittanceCase(
        14, "Contract Scope Hidden in Missing Schedule C (UK Corp)",
        "2026-10-15", "2026-10-20", "UK", "COMPANY", 1_100_000, 0, True, True, True, False,
        "[SYNTHETIC INVOICE #UK-14]: Milestone 2 payment pursuant to Master Agreement - INR 11,00,000.",
        "[SYNTHETIC CONTRACT CLAUSE 1.1]: Vendor shall perform the deliverables and grant the rights specified in Schedule C (Note: Schedule C is not attached to this document).",
        "needs review"),
]

HOLDOUT_CASES: List[RemittanceCase] = [
    RemittanceCase(
        15, "Canadian illustrations with reproduction rights", "2026-11-03", "2026-11-10", "CA", "COMPANY",
        540_000, 0, None, None, None, None,
        "[SYNTHETIC INVOICE #CA-15]: Commission for three product illustrations and right to reproduce them in packaging distributed in India - INR 5,40,000.",
        "[SYNTHETIC CONTRACT CLAUSE 2.1]: Artist will deliver three final illustrations and grants Client an exclusive, perpetual right to reproduce and adapt them in product packaging. No separate fee is assigned to creation and rights.",
        None),
    RemittanceCase(
        16, "Singapore operator training without manuals or licence", "2026-11-06", "2026-11-12", "SG", "COMPANY",
        360_000, 0, None, None, None, None,
        "[SYNTHETIC INVOICE #SG-16]: Two-day training for Indian plant operators on routine operation of the customer-owned packaging line - INR 3,60,000.",
        "[SYNTHETIC CONTRACT CLAUSE 3.1]: Trainer will demonstrate the line's standard controls. No technical documents, process rights, software access, or continuing support are provided.",
        None),
    RemittanceCase(
        17, "German equipment lease with separately priced maintenance", "2026-11-15", "2026-11-20", "DE", "COMPANY",
        920_000, 0, None, None, None, None,
        "[SYNTHETIC INVOICE #DE-17]: Optical inspection equipment lease: INR 8,00,000; preventive maintenance visits: INR 1,20,000.",
        "[SYNTHETIC CONTRACT CLAUSE 4.2]: Equipment rental and maintenance are separately priced. Maintenance is limited to routine cleaning and replacement of standard consumables; no training or transfer of technical documents is included.",
        None),
    RemittanceCase(
        18, "Australian travel reimbursement with one missing receipt", "2026-11-21", "2026-11-25", "AU", "COMPANY",
        214_000, 0, None, None, None, None,
        "[SYNTHETIC INVOICE #AU-18]: Reimbursement of travel expenses at exact cost, no markup - INR 2,14,000.",
        "[SYNTHETIC CONTRACT CLAUSE 5.1]: Client will reimburse actual airfare, lodging, and local transport against original supplier invoices, without markup. Airfare and lodging receipts are supplied; local transport receipt is missing.",
        None),
    RemittanceCase(
        19, "Singapore advertising excluded from India", "2026-12-01", "2026-12-05", "SG", "COMPANY",
        410_000, 0, None, None, None, None,
        "[SYNTHETIC INVOICE #SG-19]: Display-ad placement for a travel campaign shown only to users located in Malaysia, Thailand, and Indonesia - INR 4,10,000.",
        "[SYNTHETIC CONTRACT CLAUSE 1.1]: Publisher will exclude India from geographic targeting and will not provide Indian-user impressions. Publisher has no Indian office, staff, server, or dependent representative.",
        None),
    RemittanceCase(
        20, "UK market research and introductions without order authority", "2026-12-08", "2026-12-12", "UK", "COMPANY",
        580_000, 0, None, None, None, None,
        "[SYNTHETIC INVOICE #UK-20]: Quarterly fee for market research and a list of potential Indian distributors - INR 5,80,000.",
        "[SYNTHETIC CONTRACT CLAUSE 2.2]: Consultant identifies prospects and arranges introductions from London. It cannot negotiate prices, accept orders, or bind the vendor; the vendor independently approves every sale.",
        None),
    RemittanceCase(
        21, "US trademark licence across the 1 April transition", "2026-03-31", "2026-04-01", "US", "COMPANY",
        480_000, 0, None, None, None, None,
        "[SYNTHETIC INVOICE #US-21]: Annual trademark licence fee credited 31 March 2026 and remitted 1 April 2026 - INR 4,80,000.",
        "[SYNTHETIC CONTRACT CLAUSE 6.1]: Licensee receives the right to display the registered mark on goods sold in India for the annual term.",
        None),
    RemittanceCase(
        22, "Netherlands hosted application with separately priced consulting", "2026-12-15", "2026-12-20", "NL", "COMPANY",
        800_000, 0, None, None, None, None,
        "[SYNTHETIC INVOICE #NL-22]: Hosted inventory application access: INR 6,00,000; implementation consulting: INR 2,00,000.",
        "[SYNTHETIC CONTRACT CLAUSE 1.2]: Application access is provided through a browser. Consulting is separately scoped and priced for configuration advice. No source code, copyright, or independent software rights are transferred.",
        None),
]

def baseline_keyword_rule(case: RemittanceCase) -> str:
    text = (case.invoice_text + " " + case.contract_text).lower()
    if "reimbursement" in text:
        return "reimbursement"
    if any(w in text for w in ("patent", "trademark", "royalty", "software", "equipment")):
        return "royalty"
    if any(w in text for w in ("training", "security", "engineering", "blueprint")):
        return "fees for technical services"
    if "banner" in text or "commission" in text:
        return "business income"
    return NR

def baseline_abstain_all(case: RemittanceCase) -> str:
    return NR
