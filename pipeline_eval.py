"""
pipeline_eval.py - CLI entry point for the remittance evaluation prototype.
Keep remittance_cases.py, remittance_sources.py, and remittance_pipeline.py beside this file.

The CLI can evaluate synthetic fixed cases or run one user-entered synthetic case.
It does not determine tax treatment or rates.
"""
import argparse
import json
import math
import re
import sys
import time
from datetime import date, datetime
from typing import Dict, List, Optional
from urllib.parse import urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from remittance_cases import (
    CLOSED_LABELS, ERR, HOLDOUT_CASES, NR, TEST_CASES, RemittanceCase,
    baseline_abstain_all, baseline_keyword_rule, case_evidence_inventory,
    case_has_independent_blind_review,
    case_is_adjudicated,
    gold_routing_label,
)
from remittance_sources import (
    SOURCE_REGISTRY, TREATY_INDEX, _get_legal_source,
    check_cited_references, check_citation_roots_in_source_text, step3_lookup_legal_sources,
)
from remittance_pipeline import (
    api_usage_summary, configure_api_budget, get_api_key,
    preflight_api_dns, step1_pick_law_and_forms, step5_review_and_routing,
    steps2_and_4_extract_and_propose,
)

MAX_CONSECUTIVE_ERRORS = 3

def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def compute_metrics(preds: List[str], golds: List[str]) -> Dict:
    if len(preds) != len(golds):
        raise ValueError(f"Prediction/gold length mismatch: {len(preds)} != {len(golds)}")
    n = len(golds)
    if n == 0:
        raise ValueError("Cannot compute benchmark metrics for an empty case set")
    pairs = list(zip(preds, golds))
    return {
        "n": n,
        "accuracy_pct": round(100 * sum(1 for p, g in pairs if p == g) / n, 1),
        "confident_wrong": f"{sum(1 for p, g in pairs if p not in (g, NR, ERR))}/{n}",
        "  missed_an_abstain_case": sum(1 for p, g in pairs if g == NR and p not in (NR, ERR)),
        "  wrong_label_on_answerable": sum(1 for p, g in pairs if g != NR and p not in (g, NR, ERR)),
        "unnecessary_abstentions": sum(1 for p, g in pairs if p == NR and g != NR),
        "correct_abstentions": sum(1 for p, g in pairs if p == NR and g == NR),
        "api_errors": sum(1 for p, _ in pairs if p == ERR),
    }

def compute_axis_metrics(nature_preds: List[str], review_preds: List[Optional[bool]], cases: List[RemittanceCase]) -> Dict:
    if not (len(nature_preds) == len(review_preds) == len(cases)):
        raise ValueError("Axis predictions and cases must have the same length")
    valid = [i for i, pred in enumerate(nature_preds) if pred != ERR and review_preds[i] is not None]
    if not valid:
        return {"n": 0, "payment_nature_accuracy_pct": None, "review_decision_accuracy_pct": None}
    nature_correct = sum(nature_preds[i] == cases[i].gold_payment_nature for i in valid)
    review_correct = sum(review_preds[i] == cases[i].gold_review_required for i in valid)
    return {
        "n": len(valid),
        "payment_nature_accuracy_pct": round(100 * nature_correct / len(valid), 1),
        "review_decision_accuracy_pct": round(100 * review_correct / len(valid), 1),
        "missed_review_flags": sum(bool(cases[i].gold_review_required) and not review_preds[i] for i in valid),
        "unnecessary_review_flags": sum(not cases[i].gold_review_required and bool(review_preds[i]) for i in valid),
    }

def report_scorecard(name: str, route_preds: List[str], nature_preds: List[str],
                    review_preds: List[Optional[bool]], cases: List[RemittanceCase], rep):
    """Report every method on the same adjudicated routing and independent-axis targets."""
    route_golds = [gold_routing_label(case) for case in cases]
    rep.say(f"[{name}] final-route score (gold_routing_label):", compute_metrics(route_preds, route_golds))
    rep.say(f"[{name}] payment-nature / review-flag scores:",
            compute_axis_metrics(nature_preds, review_preds, cases))

def baseline_axis_predictions(route_preds: List[str]):
    """Interpret a single-label baseline consistently: abstain means uncertain + review."""
    nature_preds = ["uncertain" if pred == NR else pred for pred in route_preds]
    review_preds = [pred == NR for pred in route_preds]
    return nature_preds, review_preds

def case_error_labels(case: RemittanceCase, route_pred: str, nature_pred: str,
                      review_pred: Optional[bool]) -> List[str]:
    errors = []
    route_gold = gold_routing_label(case)
    if route_pred == ERR:
        errors.append("API error")
    elif route_pred != route_gold:
        errors.append(f"route expected {route_gold!r}, got {route_pred!r}")
    if nature_pred != ERR and nature_pred != case.gold_payment_nature:
        errors.append(f"nature expected {case.gold_payment_nature!r}, got {nature_pred!r}")
    if review_pred is not None and review_pred != case.gold_review_required:
        errors.append(f"review flag expected {case.gold_review_required!r}, got {review_pred!r}")
    return errors

class Reporter:
    def __init__(self, path: str):
        self.f = open(path, "w", encoding="utf-8")

    def say(self, *parts):
        line = " ".join(str(p) for p in parts)
        print(line)
        self.f.write(line + "\n")
        self.f.flush()


_CUSTOM_EVIDENCE_FIELDS = {
    "tax_residency_certificate": "has_trc",
    "portal_treaty_form": "has_portal_treaty_form",
    "no_pe_declaration": "has_no_pe_declaration",
    "section_395_certificate": "has_ao_sec395_certificate",
}


def load_custom_case(path: str) -> RemittanceCase:
    """Load one judge-entered case. It has no gold labels and is never benchmark-scored."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read case JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Case JSON must contain one object.")

    required = {
        "name", "credit_date", "payment_date", "payee_country", "payee_type",
        "amount_inr", "same_tax_year_payee_taxable_agg_inr", "invoice_text",
        "contract_text", "evidence",
    }
    missing = sorted(required - set(payload))
    extra = sorted(set(payload) - required)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing fields: {missing}")
        if extra:
            details.append(f"unknown fields: {extra}")
        raise ValueError("Invalid case JSON (" + "; ".join(details) + ").")

    for field in ("credit_date", "payment_date"):
        try:
            date.fromisoformat(payload[field])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be a date in YYYY-MM-DD format.") from exc

    country = str(payload["payee_country"]).strip().upper()
    payee_type = str(payload["payee_type"]).strip().upper()
    if not country or country != "NONE" and len(country) != 2:
        raise ValueError("payee_country must be a two-letter country code or NONE.")
    if payee_type not in {"COMPANY", "INDIVIDUAL"}:
        raise ValueError("payee_type must be COMPANY or INDIVIDUAL.")
    if not isinstance(payload["name"], str) or not payload["name"].strip():
        raise ValueError("name must be a non-empty string.")
    for field in ("invoice_text", "contract_text"):
        if not isinstance(payload[field], str) or not payload[field].strip():
            raise ValueError(f"{field} must be a non-empty string.")

    amounts = {}
    for field in ("amount_inr", "same_tax_year_payee_taxable_agg_inr"):
        value = payload[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{field} must be a finite non-negative number.")
        amounts[field] = float(value)

    raw_evidence = payload["evidence"]
    if not isinstance(raw_evidence, dict) or set(raw_evidence) != set(_CUSTOM_EVIDENCE_FIELDS):
        raise ValueError("evidence must include exactly the four document keys shown in judge_case_template.json.")
    inventory = {}
    evidence_values = {}
    for document, field_name in _CUSTOM_EVIDENCE_FIELDS.items():
        item = raw_evidence[document]
        if not isinstance(item, dict):
            raise ValueError(f"evidence.{document} must be an object with status and provenance.")
        status = item.get("status")
        source = item.get("source")
        detail = item.get("source_detail", "")
        if status not in {"supplied", "missing", "unknown"}:
            raise ValueError(f"evidence.{document}.status must be supplied, missing, or unknown.")
        if not isinstance(source, str) or not source.strip() or not isinstance(detail, str):
            raise ValueError(f"evidence.{document} needs a non-empty source and optional text source_detail.")
        inventory[document] = {
            "status": status,
            "source": source.strip(),
            "source_detail": detail.strip(),
        }
        evidence_values[field_name] = {"supplied": True, "missing": False, "unknown": None}[status]

    return RemittanceCase(
        case_id=0,
        name=payload["name"].strip(),
        credit_date=payload["credit_date"],
        payment_date=payload["payment_date"],
        payee_country=country,
        payee_type=payee_type,
        amount_inr=amounts["amount_inr"],
        same_tax_year_payee_taxable_agg_inr=amounts["same_tax_year_payee_taxable_agg_inr"],
        invoice_text=payload["invoice_text"].strip(),
        contract_text=payload["contract_text"].strip(),
        gold_label=None,
        input_evidence_inventory=inventory,
        **evidence_values,
    )


def run_custom_case(case: RemittanceCase, model: str, include_source_context: bool,
                    output_path: Optional[str] = None) -> Dict:
    """Run one unscored user case and print the reviewer-facing result as JSON."""
    s1 = step1_pick_law_and_forms(case.credit_date, case.payment_date)
    legal_lookup = step3_lookup_legal_sources(s1, case.payee_country)
    llm_out = steps2_and_4_extract_and_propose(
        case, legal_lookup, model=model, include_source_context=include_source_context
    )
    result = {
        "case_name": case.name,
        "mode": "single case with source context in prompt" if include_source_context
                else "single case without source context in prompt",
        "benchmark_score": "not computed; no reviewed gold label supplied",
        "transaction": {
            "credit_date": case.credit_date,
            "payment_date": case.payment_date,
            "payee_country": case.payee_country,
            "payee_type": case.payee_type,
            "amount_inr": case.amount_inr,
            "same_tax_year_payee_taxable_agg_inr": case.same_tax_year_payee_taxable_agg_inr,
        },
        "model_requested": model,
        "served_model": llm_out.get("served_model"),
        "api_error": llm_out.get("api_error"),
        "payment_nature": None,
        "review_required": None,
        "final_route": None,
        "review_reasons": [],
        "evidence_inventory": case_evidence_inventory(case),
        "api_usage": api_usage_summary(),
        "notice": "Reviewer aid only. Verify original documents, legal sources, facts, and legal application before any tax action.",
    }
    if not llm_out.get("api_error"):
        routed = step5_review_and_routing(case, s1, llm_out, legal_lookup)
        registry_present, registry_missing = check_cited_references(llm_out.get("cited_clauses", ""), legal_lookup)
        source_text_present, source_text_missing = check_citation_roots_in_source_text(
            llm_out.get("cited_clauses", ""), legal_lookup
        )
        result.update({
            "payment_nature": routed["payment_nature"],
            "review_required": routed["review_required"],
            "final_route": routed["final_label"],
            "review_reasons": routed["review_reasons"],
            "document_quotes_verified": routed["document_quotes_verified"],
            "extracted_invoice_quote": llm_out.get("invoice_quote", ""),
            "extracted_contract_quote": llm_out.get("contract_quote", ""),
            "cited_provisions": llm_out.get("cited_clauses", ""),
            "citation_present_in_registry": registry_present,
            "citations_missing_from_registry": registry_missing,
            "citation_present_in_source_text": source_text_present,
            "citation_roots_missing_from_source_text": source_text_missing,
            "legal_sources": routed["legal_sources"],
            "procedural_rule_used": routed["procedural_rule_used"],
            "form_route": routed["form_route"],
            "gate_notes": routed["gate_notes"],
        })
    if output_path:
        result["saved_result"] = output_path
        with open(output_path, "x", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result

def run_mode(include_source_context: bool, mode_name: str, model: str, runs: int, sleep_s: float, log, rep: Reporter,
             cases: List[RemittanceCase]):
    golds = [gold_routing_label(c) for c in cases]
    all_runs, all_nature_runs, all_review_runs = [], [], []
    served_models = set()
    cit_strict, cit_ws, clause_ok, citation_text_ok, total = 0, 0, 0, 0, 0
    consecutive_errors = 0

    for run_idx in range(1, runs + 1):
        preds = []
        nature_preds = []
        review_preds: List[Optional[bool]] = []
        for case in cases:
            s1 = step1_pick_law_and_forms(case.credit_date, case.payment_date)
            s3 = step3_lookup_legal_sources(s1, case.payee_country)
            llm_out = steps2_and_4_extract_and_propose(
                case, s3, model=model, include_source_context=include_source_context
            )
            s5 = step5_review_and_routing(case, s1, llm_out, s3)

            is_error = bool(llm_out.get("api_error"))
            if not is_error:
                served_models.add(str(llm_out.get("served_model") or "unknown"))
            consecutive_errors = consecutive_errors + 1 if is_error else 0
            preds.append(ERR if is_error else s5["final_label"])
            nature_preds.append(ERR if is_error else s5["payment_nature"])
            review_preds.append(None if is_error else s5["review_required"])

            if is_error:
                ok, missing = False, ["api_error"]
            else:
                total += 1
                cit_strict += int(s5["document_quotes_verified"])
                iq, cq = _ws(llm_out.get("invoice_quote", "")), _ws(llm_out.get("contract_quote", ""))
                cit_ws += int(len(iq) >= 10 and iq in _ws(case.invoice_text) and len(cq) >= 10 and cq in _ws(case.contract_text))
                ok, missing = check_cited_references(llm_out.get("cited_clauses", ""), s3)
                clause_ok += int(ok)
                text_ok, text_missing = check_citation_roots_in_source_text(llm_out.get("cited_clauses", ""), s3)
                citation_text_ok += int(text_ok)

            log.write(json.dumps({
                "mode": mode_name, "run": run_idx, "case_id": case.case_id,
                "gold_legacy_label": case.gold_label,
                "gold_routing_label": gold_routing_label(case),
                "gold_payment_nature": case.gold_payment_nature,
                "gold_review_required": case.gold_review_required,
                "gold_independent_blind_review_recorded": case_has_independent_blind_review(case),
                "case_evidence_inventory": case_evidence_inventory(case),
                "final_label": preds[-1], "raw_llm_output": llm_out, "step5": s5,
                "citation_present_in_registry": ok, "citation_missing_from_registry": missing,
                "citation_present_in_source_text": text_ok if not is_error else False,
                "citation_missing_from_source_text": text_missing if not is_error else ["api_error"],
            }) + "\n")
            log.flush()
            print(
                f"  [{mode_name}] run {run_idx} case {case.case_id:>2}: "
                f"gold=(nature={case.gold_payment_nature!r}, review={case.gold_review_required!r}, "
                f"route={gold_routing_label(case)!r}) "
                f"got=(nature={nature_preds[-1]!r}, review={review_preds[-1]!r}, route={preds[-1]!r})"
            )
            if is_error:
                print(f"      reason: {llm_out.get('reasoning', '')[:300]}")

            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                sys.exit(f"ABORT: {consecutive_errors} API failures in a row. Last: {llm_out.get('reasoning')}")
            time.sleep(sleep_s)

        all_runs.append(preds)
        rep.say(f"[{mode_name}] final-route (gold_routing_label) run {run_idx}:", compute_metrics(preds, golds))
        rep.say(
            f"[{mode_name}] payment-nature / review-flag run {run_idx}:",
            compute_axis_metrics(nature_preds, review_preds, cases),
        )
        rep.say(f"[{mode_name}] per-case errors run {run_idx}:")
        error_count = 0
        for case, route_pred, nature_pred, review_pred in zip(cases, preds, nature_preds, review_preds):
            errors = case_error_labels(case, route_pred, nature_pred, review_pred)
            if errors:
                error_count += 1
                rep.say(f"   case {case.case_id}: " + "; ".join(errors))
        if not error_count:
            rep.say("   none")
        all_nature_runs.append(nature_preds)
        all_review_runs.append(review_preds)

    consistent = sum(1 for i in range(len(cases)) if len({r[i] for r in all_runs}) == 1)
    rep.say(f"[{mode_name}] identical label across {runs} run(s): {consistent}/{len(cases)} cases")
    nature_consistent = sum(1 for i in range(len(cases)) if len({r[i] for r in all_nature_runs}) == 1)
    review_consistent = sum(1 for i in range(len(cases)) if len({r[i] for r in all_review_runs}) == 1)
    rep.say(
        f"[{mode_name}] identical payment nature / review decision across {runs} run(s): "
        f"{nature_consistent}/{len(cases)} / {review_consistent}/{len(cases)} cases"
    )
    if total:
        rep.say(f"[{mode_name}] verbatim quotes strict: {cit_strict}/{total}; "
                f"after whitespace normalising (report only): {cit_ws}/{total}; "
                f"citation present in registry (not support verification): {clause_ok}/{total}; "
                f"citation present in source text (not support verification): {citation_text_ok}/{total}")
    rep.say(f"[{mode_name}] sample size: {len(cases)} cases per run")
    rep.say(f"[{mode_name}] actual served model(s): {sorted(served_models) if served_models else 'none (all calls failed)'}")
    if len(served_models) > 1:
        rep.say(f"[{mode_name}] WARNING: served model varied during this mode: {sorted(served_models)}")

def main():
    ap = argparse.ArgumentParser(description="Run the fixed-case benchmark or one reviewer-entered synthetic case.")
    ap.add_argument("--model", required=True, help="exact OpenRouter model slug; auto is not allowed for benchmark runs")
    ap.add_argument("--runs", type=int, default=1, help="runs per mode (default 1)")
    ap.add_argument("--modes", choices=["both", "with-context", "without-context"], default=None,
                    help="whether official source excerpts are included in the LLM prompt; both modes still retrieve sources for downstream checks")
    ap.add_argument("--case-file", help="run one case from JSON instead of the fixed benchmark (see judge_case_template.json)")
    ap.add_argument("--output", help="optionally save the single-case JSON result to this path; input text is not saved by default")
    ap.add_argument("--dataset", choices=["development", "holdout"], default="development",
                    help="holdout requires independent blind-review records and configured treaty sources")
    ap.add_argument("--max-api-calls", type=int, default=0,
                    help="hard cap on actual HTTP requests; 0 means one request per planned case/run/mode")
    ap.add_argument("--sleep", type=float, default=2.5, help="seconds between calls (free-tier rate limits)")
    args = ap.parse_args()
    args.model = args.model.strip()
    if not args.model or args.model.casefold() == "auto" or args.model.casefold().endswith("/auto"):
        ap.error("--model must be a specific model slug; choose an exact provider/model ID instead of 'auto'")
    if args.runs < 1:
        ap.error("--runs must be at least 1")
    if args.sleep < 0:
        ap.error("--sleep cannot be negative")
    if args.max_api_calls < 0:
        ap.error("--max-api-calls cannot be negative")
    if args.output and not args.case_file:
        ap.error("--output is only available with --case-file")

    if args.case_file:
        if args.dataset != "development":
            ap.error("--case-file cannot be combined with --dataset holdout")
        if args.runs != 1:
            ap.error("--case-file supports one run at a time")
        if args.modes == "both":
            ap.error("--case-file accepts one prompt mode per run; use --modes with-context or --modes without-context")
        try:
            case = load_custom_case(args.case_file)
        except ValueError as exc:
            ap.error(str(exc))
        if case.payee_country not in TREATY_INDEX:
            ap.error(
                f"No treaty source is configured for payee country {case.payee_country!r}; "
                "no model API request was sent."
            )
        pdf_sources = {
            source_id for source_id in TREATY_INDEX[case.payee_country]["source_ids"]
            if urlparse(SOURCE_REGISTRY[source_id]["url"]).path.casefold().endswith(".pdf")
        }
        if pdf_sources:
            try:
                import pypdf  # noqa: F401
            except ImportError:
                ap.error(f"PDF treaty sources {sorted(pdf_sources)} require pypdf; no model API request was sent.")
        budget = args.max_api_calls or 4  # one initial request plus up to three retries
        configure_api_budget(budget)
        preflight_api_dns()
        get_api_key()
        run_custom_case(
            case, args.model, include_source_context=(args.modes != "without-context"),
            output_path=args.output,
        )
        return

    args.modes = args.modes or "both"

    cases = TEST_CASES if args.dataset == "development" else HOLDOUT_CASES
    pending = [case.case_id for case in cases if not case_is_adjudicated(case)]
    if pending:
        ap.error(
            f"{args.dataset} dataset has cases without independently adjudicated labels: {pending}. "
            "No API requests were sent. Complete reviewer gold labels in the script before running it."
        )
    pending_review = [case.case_id for case in cases if not case_has_independent_blind_review(case)]
    if args.dataset == "holdout" and pending_review:
        ap.error(
            f"holdout cases lack a complete blind independent review record: {pending_review}. "
            "Record reviewer qualification, identity/role, date, rationale, source references, and confirmation "
            "that the reviewer did not see model outputs. No API requests were sent."
        )

    modes_count = 2 if args.modes == "both" else 1
    planned_calls = len(cases) * args.runs * modes_count
    api_call_budget = args.max_api_calls or planned_calls
    if api_call_budget < planned_calls and args.max_api_calls:
        print(f"Request budget {api_call_budget} is below planned calls {planned_calls}; the run may stop early.")
    configure_api_budget(api_call_budget)

    unsupported_treaty_codes = sorted({
        case.payee_country for case in cases
        if case.payee_country != "NONE" and case.payee_country not in TREATY_INDEX
    })
    if unsupported_treaty_codes:
        ap.error(
            f"{args.dataset} includes payee countries without configured treaty sources: {unsupported_treaty_codes}. "
            "Add and verify their official treaty sources before running this dataset. No API requests were sent."
        )

    pdf_treaty_sources = {
        source_id
        for case in cases
        for source_id in TREATY_INDEX.get(case.payee_country, TREATY_INDEX["NONE"])["source_ids"]
        if urlparse(SOURCE_REGISTRY[source_id]["url"]).path.casefold().endswith(".pdf")
    }
    if pdf_treaty_sources:
        try:
            import pypdf  # noqa: F401
        except ImportError:
            ap.error(
                f"PDF treaty sources {sorted(pdf_treaty_sources)} require pypdf. "
                "Install it with 'python -m pip install pypdf' before any model requests."
            )

    if args.dataset == "holdout":
        required_treaty_source_ids = sorted({
            source_id
            for case in cases
            for source_id in TREATY_INDEX.get(case.payee_country, TREATY_INDEX["NONE"])["source_ids"]
        })
        unavailable_treaty_sources = [
            source_id
            for source_id in required_treaty_source_ids
            if _get_legal_source(source_id)["retrieval_status"] == "stale"
        ]
        if unavailable_treaty_sources:
            ap.error(
                f"Holdout treaty sources could not be refreshed and have no usable cache: "
                f"{unavailable_treaty_sources}. No model API requests were sent. "
                "Check the official source URLs/network or refresh the legal cache before retrying."
            )

    preflight_api_dns()  # avoid futile per-case retries when API DNS is unavailable
    get_api_key()  # fail fast, before any work
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    rep = Reporter(f"results_summary_{stamp}.txt")
    rep.say(f"Model requested: {args.model} | dataset: {args.dataset} | runs: {args.runs} | modes: {args.modes} | started: {stamp}")
    rep.say(f"Hard API request budget: {api_call_budget} actual HTTP requests (retries count against this cap).")
    gold_status = (
        "independent blind-review record entered (qualification is self-attested, not verified by the script)"
        if all(case_has_independent_blind_review(case) for case in cases)
        else "EXPLORATORY ONLY: author-assigned labels, not independently adjudicated blind"
    )
    rep.say(f"Gold-label status: {gold_status}")
    rep.say(
        "CITATION METRICS ARE PRESENCE CHECKS ONLY: registry/source-text presence does not verify the correct source, "
        "subsection, claim support, or legal applicability. Do not report them as legal citation accuracy."
    )
    legacy_golds = [c.gold_label for c in cases]
    if all(label in CLOSED_LABELS for label in legacy_golds):
        rep.say("HISTORICAL LEGACY-LABEL BASELINES (separate target; do not compare with the scorecards below):")
        rep.say("Keyword rule vs legacy gold:", compute_metrics([baseline_keyword_rule(c) for c in cases], legacy_golds))
        rep.say("Abstain-all vs legacy gold:", compute_metrics([baseline_abstain_all(c) for c in cases], legacy_golds))
    else:
        rep.say("Historical combined-label scores skipped: this dataset has no legacy gold labels.")

    rep.say("COMPARABLE SCORECARDS (all methods below use the same route, nature, and review gold targets):")
    keyword_routes = [baseline_keyword_rule(case) for case in cases]
    keyword_natures, keyword_reviews = baseline_axis_predictions(keyword_routes)
    report_scorecard("Baseline keyword rule", keyword_routes, keyword_natures, keyword_reviews, cases, rep)
    abstain_routes = [baseline_abstain_all(case) for case in cases]
    abstain_natures, abstain_reviews = baseline_axis_predictions(abstain_routes)
    report_scorecard("Baseline abstain on all", abstain_routes, abstain_natures, abstain_reviews, cases, rep)
    rep.say("Single-label baseline convention: needs review maps to uncertain payment nature + review required; a predicted category maps to review not required.")

    path = f"raw_llm_runs_{stamp}.jsonl"
    with open(path, "x", encoding="utf-8") as log:
        log.write(json.dumps({"meta": {
            "model_requested": args.model, "dataset": args.dataset, "runs": args.runs,
            "started": stamp, "api_call_budget": api_call_budget,
            "gold_label_status": gold_status,
        }}) + "\n")
        if args.modes in ("both", "without-context"):
            run_mode(False, "Without source context in prompt", args.model, args.runs, args.sleep, log, rep, cases)
        if args.modes in ("both", "with-context"):
            run_mode(True, "With source context in prompt", args.model, args.runs, args.sleep, log, rep, cases)
        usage = api_usage_summary()
        log.write(json.dumps({"usage_summary": usage}) + "\n")
        log.flush()
        rep.say("API/token usage:", usage)

    rep.say(f"Raw, unedited model outputs saved to {path} (each line includes 'served_model').")
    rep.say("Reminder: retrieved source text and citation presence checks are evidence only; neither confirms that a provision supports the claim or applies legally.")
    if "EXPLORATORY ONLY" in gold_status:
        rep.say("Do not present these scores as validated performance until a qualified reviewer independently adjudicates the gold labels blind to model outputs.")


if __name__ == "__main__":
    main()
