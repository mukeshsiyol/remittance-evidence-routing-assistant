"""Official-source registry, retrieval/cache helpers, and citation-presence checks."""
import hashlib
import json
import os
import re
import socket
from datetime import datetime, timezone
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

import requests

SOURCE_FRESHNESS_DAYS = 30
LEGAL_CACHE_DIR = Path(__file__).resolve().parent / "legal_cache"
MAX_SOURCE_BYTES = 2_000_000
ALLOWED_LEGAL_HOSTS = {
    "wm.incometaxindia.gov.in",
    "wmstatic-prd.incometaxindia.gov.in",
    "incometaxindia.gov.in",
    "www.gov.uk",
    "www.ato.gov.au",
}
LEGAL_SOURCE_RUNTIME_CACHE: Dict[str, Dict] = {}
LEGAL_SOURCE_TEXT_RUNTIME_CACHE: Dict[str, str] = {}

SOURCE_REGISTRY = {
    "ACT_1961_195": {
        "title": "Income-tax Act, 1961 - Section 195",
        "authority": "Income Tax Department, Government of India",
        "url": "https://wm.incometaxindia.gov.in/w/section-195-36",
        "provisions": ["Section 195"],
        "verification_terms": ["195. (1)"],
        "retrieval_terms": ["195. (1)"],
        "effective_from": "1961-04-01",
        "effective_to": "2026-03-31",
        "source_version": "Income-tax Act, 1961; refer to amendments effective for the transaction date",
    },
    "ACT_1961_9": {
        "title": "Income-tax Act, 1961 - Section 9",
        "authority": "Income Tax Department, Government of India",
        "url": "https://wm.incometaxindia.gov.in/w/section-9-56",
        "provisions": ["Section 9(1)(i)", "Section 9(1)(vi)", "Section 9(1)(vii)"],
        "verification_terms": ["9. (1)"],
        "retrieval_terms": ["business connection in India", "income by way of royalty", "fees for technical services"],
        "effective_from": "1961-04-01",
        "effective_to": "2026-03-31",
        "source_version": "Income-tax Act, 1961; refer to amendments effective for the transaction date",
    },
    "ACT_2025_9": {
        "title": "Income-tax Act, 2025 - Section 9",
        "authority": "Income Tax Department, Government of India",
        "url": "https://wm.incometaxindia.gov.in/w/section-9-1",
        "provisions": ["Section 9(6)", "Section 9(7)", "Section 9(9)"],
        "verification_terms": ["income by way of royalty payable by", "income by way of fees for technical services payable by"],
        "retrieval_terms": ["income by way of royalty payable by", "income by way of fees for technical services payable by", "significant economic presence"],
        "effective_from": "2026-04-01",
        "effective_to": None,
        "source_version": "Income-tax Act, 2025; verify amendments effective for the transaction date",
    },
    "ACT_2025_393": {
        "title": "Income-tax Act, 2025 - Section 393",
        "authority": "Income Tax Department, Government of India",
        "url": "https://wm.incometaxindia.gov.in/w/section-393-5",
        "provisions": ["Section 393(2)"],
        "verification_terms": ["Section 393", "FOR PAYMENTS TO NON-RESIDENT", "Any interest"],
        "retrieval_terms": ["FOR PAYMENTS TO NON-RESIDENT", "Any interest (not being interest", "Sl. No. 17"],
        "effective_from": "2026-04-01",
        "effective_to": None,
        "source_version": "Income-tax Act, 2025; verify amendments effective for the transaction date",
    },
    "ACT_2025_159": {
        "title": "Income-tax Act, 2025 - Section 159",
        "authority": "Income Tax Department, Government of India",
        "url": "https://wm.incometaxindia.gov.in/w/section-159-85",
        "provisions": ["Section 159(8)"],
        "verification_terms": ["Section 159", "An assessee, not being a resident, shall be entitled to claim any relief"],
        "retrieval_terms": ["An assessee, not being a resident, shall be entitled to claim any relief"],
        "effective_from": "2026-04-01",
        "effective_to": None,
        "source_version": "Income-tax Act, 2025; verify amendments effective for the transaction date",
    },
    "RULES_1962": {
        "title": "Income-tax Rules, 1962 - Rule 37BB and Forms 15CA/15CB",
        "authority": "Income Tax Department, Government of India",
        "url": "https://wm.incometaxindia.gov.in/w/rule-37bb",
        "provisions": ["Rule 37BB", "Form 15CA", "Form 15CB"],
        "verification_terms": ["37BB. (1)"],
        "retrieval_terms": ["37BB. (1)"],
        "effective_from": None,
        "effective_to": "2026-03-31",
        "source_version": "Consolidated rule page; verify amendments effective for the transaction date",
    },
    "RULES_2026": {
        "title": "Income-tax Rules, 2026 - Rule 220 and Forms 145/146",
        "authority": "Income Tax Department, Government of India; notified rules",
        "url": "https://wm.incometaxindia.gov.in/w/rule-220-1",
        "provisions": ["Rule 220", "Form 145", "Form 146", "Rule 75", "Form 41"],
        "verification_terms": ["220. (1)"],
        "retrieval_terms": ["220. (1)"],
        "effective_from": "2026-04-01",
        "effective_to": None,
        "source_version": "Consolidated rule page from the Income-tax Rules, 2026; verify subsequent amendments",
    },
    "TREATY_US": {
        "title": "India-USA income tax convention and related documents",
        "authority": "Income Tax Department, Government of India",
        "url": "https://wm.incometaxindia.gov.in/w/usa-comprehensive-agreements-1",
        "provisions": ["Article 7", "Article 12", "Article 15"],
        "verification_terms": ["ARTICLE 12 ROYALTIES AND FEES FOR INCLUDED SERVICES"],
        "retrieval_terms": ["ARTICLE 7 BUSINESS PROFITS", "ARTICLE 12 ROYALTIES AND FEES FOR INCLUDED SERVICES", "ARTICLE 15 INDEPENDENT PERSONAL SERVICES"],
        "effective_from": None,
        "effective_to": None,
        "source_version": "Convention and related official documents; verify protocols and effective dates",
    },
    "TREATY_UK": {
        "title": "2020 UK-India synthesised text of the MLI and 1993 tax convention",
        "authority": "HM Revenue & Customs, Government of the United Kingdom",
        "url": "https://www.gov.uk/government/publications/india-tax-treaties/2020-uk-india-synthesised-text-of-the-multilateral-instrument-and-the-1993-double-taxation-convention-in-force",
        "provisions": ["Article 7", "Article 13"],
        "verification_terms": ["Royalties and fees for technical services arising in a Contracting State and paid to a resident"],
        "retrieval_terms": ["The profits of an enterprise of a Contracting State shall be taxable only", "Royalties and fees for technical services arising in a Contracting State and paid to a resident"],
        "effective_from": None,
        "effective_to": None,
        "source_version": "2020 synthesised text; verify current protocol and article applicability",
    },
    "TREATY_SG": {
        "title": "India-Singapore synthesised tax treaty text",
        "authority": "Income Tax Department, Government of India",
        "url": "https://wm.incometaxindia.gov.in/w/singapore-synthesised-text-1",
        "provisions": ["Article 7", "Article 12", "Article 14"],
        "verification_terms": ["ARTICLE 12 ROYALTIES AND FEES FOR TECHNICAL SERVICES"],
        "retrieval_terms": ["ARTICLE 7 BUSINESS PROFITS", "ARTICLE 12 ROYALTIES AND FEES FOR TECHNICAL SERVICES", "ARTICLE 14 INDEPENDENT PERSONAL SERVICES"],
        "effective_from": None,
        "effective_to": None,
        "source_version": "Official synthesised text incorporating listed protocols and MLI; verify current application",
    },
    "TREATY_CA": {
        "title": "India-Canada synthesized treaty text with MLI",
        "authority": "Income Tax Department, Government of India; prepared in consultation with Canada",
        "url": "https://incometaxindia.gov.in/dtaa/synthesised-text-of-the-mli-and-india-canada-dtaa.pdf",
        "provisions": ["Article 7", "Article 12", "Article 14"],
        "verification_terms": ["SYNTHESISED TEXT", "ARTICLE 12", "ROYALTIES AND FEES FOR INCLUDED SERVICES"],
        "retrieval_terms": ["ARTICLE 7 BUSINESS PROFITS", "ARTICLE 12 ROYALTIES AND FEES FOR INCLUDED SERVICES", "ARTICLE 14 INDEPENDENT PERSONAL SERVICES"],
        "effective_from": None,
        "effective_to": None,
        "source_version": "India-Canada MLI synthesized text; its notice says authentic treaty/MLI texts prevail and MLI effects vary by tax type and date",
    },
    "TREATY_AU": {
        "title": "India-Australia synthesized treaty text with MLI",
        "authority": "Income Tax Department, Government of India; prepared in consultation with Australia",
        "url": "https://wmstatic-prd.incometaxindia.gov.in/documents/20117/42998/Australia-Synthesised-Text_2025-12-16_01-52-55_5d6f78_en.pdf/9d21f9ca-6a2c-1a55-dbfa-ae3ca91ff5c7?download=true&t=1774106292518&version=1.0",
        "provisions": ["Article 7", "Article 12", "Article 14"],
        "verification_terms": ["SYNTHESISED TEXT", "ARTICLE 12", "ROYALTIES"],
        "retrieval_terms": ["ARTICLE 7 BUSINESS PROFITS", "ARTICLE 12 ROYALTIES", "ARTICLE 14 INDEPENDENT PERSONAL SERVICES"],
        "effective_from": None,
        "effective_to": None,
        "source_version": "India-Australia MLI synthesized text dated 2025-12-16; confirm authentic text and tax-specific MLI effective dates",
    },
    "TREATY_DE": {
        "title": "India-Germany double taxation agreement",
        "authority": "Income Tax Department, Government of India",
        "url": "https://wm.incometaxindia.gov.in/w/germany-comprehensive-agreements-1",
        "provisions": ["Article 7", "Article 12", "Article 14"],
        "verification_terms": ["ARTICLE 12", "ROYALTIES"],
        "retrieval_terms": ["ARTICLE 7", "ARTICLE 12", "ARTICLE 14"],
        "effective_from": None,
        "effective_to": None,
        "source_version": "Official India treaty text page; verify subsequent protocols, MLI application and effective dates",
    },
    "TREATY_NL": {
        "title": "India-Netherlands synthesized treaty text with MLI",
        "authority": "Income Tax Department, Government of India, prepared in consultation with the Netherlands",
        "url": "https://wm.incometaxindia.gov.in/w/netherlands-synthesised-text-1",
        "provisions": ["Article 7", "Article 12", "Article 14"],
        "verification_terms": ["SYNTHESISED TEXT", "ARTICLE 12", "ROYALTIES"],
        "retrieval_terms": ["ARTICLE 7", "ARTICLE 12", "ARTICLE 14"],
        "effective_from": None,
        "effective_to": None,
        "source_version": "India-Netherlands MLI synthesized text; its notice says authentic treaty/MLI texts prevail and MLI effects vary by tax type and date",
    },
}

STATUTORY_INDEX = {
    "ACT_1961": {
        "act_name": "Income-tax Act, 1961 (for earlier triggering events)",
        "citation_refs": SOURCE_REGISTRY["ACT_1961_195"]["provisions"] + SOURCE_REGISTRY["ACT_1961_9"]["provisions"],
        "source_ids": ["ACT_1961_195", "ACT_1961_9"],
    },
    "ACT_2025": {
        "act_name": "Income-tax Act, 2025 (from 1 April 2026)",
        "citation_refs": SOURCE_REGISTRY["ACT_2025_9"]["provisions"] + SOURCE_REGISTRY["ACT_2025_393"]["provisions"] + SOURCE_REGISTRY["ACT_2025_159"]["provisions"],
        "source_ids": ["ACT_2025_9", "ACT_2025_393", "ACT_2025_159"],
    },
}

PROCEDURAL_FORM_INDEX = {
    "PRE_APR_2026": {
        "rule": "Rule 37BB (Income-tax Rules, 1962)",
        "citation_refs": SOURCE_REGISTRY["RULES_1962"]["provisions"],
        "source_ids": ["RULES_1962"],
        "threshold_inr": 500_000,
        "remitter_form": "Form 15CA",
        "ca_form": "Form 15CB",
        "treaty_decl_form": "Form 10F (Rule 21AB)",
    },
    "POST_APR_2026": {
        "rule": "Rule 220 (Income-tax Rules, 2026)",
        "citation_refs": SOURCE_REGISTRY["RULES_2026"]["provisions"],
        "source_ids": ["RULES_2026"],
        "threshold_inr": 500_000,
        "remitter_form": "Form 145",
        "ca_form": "Form 146",
        "treaty_decl_form": "Form 41 (Rule 75)",
    },
}

TREATY_INDEX = {
    "US": {"name": "India-USA DTAA", "citation_refs": SOURCE_REGISTRY["TREATY_US"]["provisions"], "source_ids": ["TREATY_US"]},
    "UK": {"name": "India-UK DTAA", "citation_refs": SOURCE_REGISTRY["TREATY_UK"]["provisions"], "source_ids": ["TREATY_UK"]},
    "SG": {"name": "India-Singapore DTAA", "citation_refs": SOURCE_REGISTRY["TREATY_SG"]["provisions"], "source_ids": ["TREATY_SG"]},
    "CA": {"name": "India-Canada DTAA", "citation_refs": SOURCE_REGISTRY["TREATY_CA"]["provisions"], "source_ids": ["TREATY_CA"]},
    "AU": {"name": "India-Australia DTAA", "citation_refs": SOURCE_REGISTRY["TREATY_AU"]["provisions"], "source_ids": ["TREATY_AU"]},
    "DE": {"name": "India-Germany DTAA", "citation_refs": SOURCE_REGISTRY["TREATY_DE"]["provisions"], "source_ids": ["TREATY_DE"]},
    "NL": {"name": "India-Netherlands DTAA", "citation_refs": SOURCE_REGISTRY["TREATY_NL"]["provisions"], "source_ids": ["TREATY_NL"]},
    "NONE": {"name": "No treaty source configured for this test case", "citation_refs": [], "source_ids": []},
}

class _VisibleTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []

    def handle_data(self, data: str):
        if data.strip():
            self.parts.append(data.strip())

def _html_to_text(content: bytes, encoding: Optional[str]) -> str:
    markup = content.decode(encoding or "utf-8", errors="replace")
    markup = re.sub(r"(?is)<(script|style|noscript|svg)\b[^>]*>.*?</\1\s*>", " ", markup)
    parser = _VisibleTextParser()
    parser.feed(markup)
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()

def _source_content_to_text(content: bytes, content_type: str, encoding: Optional[str]) -> str:
    """Extract readable text from allowlisted HTML pages and official treaty PDFs."""
    if "pdf" in (content_type or "").casefold() or content.startswith(b"%PDF-"):
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ValueError("pypdf is required to retrieve PDF treaty sources; install it with python -m pip install pypdf") from exc
        reader = PdfReader(BytesIO(content), strict=False)
        text = " ".join(page.extract_text() or "" for page in reader.pages)
        return re.sub(r"\s+", " ", text).strip()
    if "html" not in (content_type or "").casefold():
        raise ValueError("Expected an HTML page or readable PDF source")
    return _html_to_text(content, encoding)

def _source_excerpt(source: Dict, text: str) -> str:
    terms = source.get("retrieval_terms") or source.get("verification_terms", [])
    lower = text.lower()
    spans = []
    is_treaty = source["source_id"].startswith("TREATY_")
    for term in terms:
        start_at = lower.find(term.lower())
        if start_at < 0:
            continue
        start = max(0, start_at - 180)
        end = min(len(text), start_at + (4200 if is_treaty else 3200))
        spans.append((start, end))
    if not spans:
        return ""
    spans.sort()
    merged = []
    for start, end in spans:
        if merged and start <= merged[-1][1] + 120:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return "\n[... omitted unrelated source text ...]\n".join(text[a:b] for a, b in merged)[:14000]

def _valid_source_text(source: Dict, text: str) -> bool:
    if not isinstance(text, str) or len(text) < 300:
        return False
    lowered = text.casefold()
    return all(term.casefold() in lowered for term in source.get("verification_terms", []))

def _latest_cached_source(source_id: str) -> Optional[Dict]:
    if not LEGAL_CACHE_DIR.exists():
        return None
    for path in sorted(LEGAL_CACHE_DIR.glob(f"{source_id}--*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(cached, dict):
                continue
            full_text = cached.get("full_text", "")
            if not isinstance(full_text, str):
                continue
            digest = hashlib.sha256(full_text.encode("utf-8")).hexdigest()
            if digest == cached.get("content_sha256") and _valid_source_text(SOURCE_REGISTRY[source_id], full_text):
                return cached
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return None

def _get_legal_source(source_id: str) -> Dict:
    if source_id in LEGAL_SOURCE_RUNTIME_CACHE:
        return LEGAL_SOURCE_RUNTIME_CACHE[source_id]
    source = SOURCE_REGISTRY[source_id]
    cached = _latest_cached_source(source_id)
    if cached:
        LEGAL_SOURCE_TEXT_RUNTIME_CACHE[source_id] = cached.get("full_text", "")
    try:
        host = (urlparse(source["url"]).hostname or "").lower()
        if host not in ALLOWED_LEGAL_HOSTS:
            raise ValueError(f"Source host is not allowlisted: {host}")
        response = requests.get(
            source["url"],
            headers={"User-Agent": "RemittanceEvalPrototype/1.0", "Accept": "text/html,application/pdf"},
            timeout=(8, 25),
        )
        response.raise_for_status()
        final_host = (urlparse(response.url).hostname or "").lower()
        if final_host not in ALLOWED_LEGAL_HOSTS:
            raise ValueError(f"Source redirected outside allowlist: {final_host}")
        if len(response.content) > MAX_SOURCE_BYTES:
            raise ValueError("Source page exceeds the configured size limit")
        content_type = response.headers.get("Content-Type", "")
        full_text = _source_content_to_text(response.content, content_type, response.encoding)
        if not _valid_source_text(source, full_text):
            raise ValueError("Fetched page did not contain the expected legal provision")
        LEGAL_SOURCE_TEXT_RUNTIME_CACHE[source_id] = full_text
        retrieved_at = datetime.now(timezone.utc).isoformat()
        digest = hashlib.sha256(full_text.encode("utf-8")).hexdigest()
        cache_record = {
            "source_id": source_id,
            "url": source["url"],
            "source_version": source["source_version"],
            "retrieved_at": retrieved_at,
            "content_sha256": digest,
            "full_text": full_text,
        }
        LEGAL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path = LEGAL_CACHE_DIR / f"{source_id}--{digest[:16]}.json"
        if not cache_path.exists():
            temp_path = cache_path.with_suffix(".tmp")
            temp_path.write_text(json.dumps(cache_record, ensure_ascii=False), encoding="utf-8")
            os.replace(temp_path, cache_path)
        record = source | {
            "source_id": source_id,
            "retrieval_status": "live",
            "retrieved_at": retrieved_at,
            "content_sha256": digest,
            "text_excerpt": _source_excerpt(source | {"source_id": source_id}, full_text),
        }
    except (requests.RequestException, OSError, ValueError) as exc:
        if cached:
            retrieved_at = cached.get("retrieved_at", "")
            try:
                age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(retrieved_at)).days
            except (TypeError, ValueError):
                age_days = SOURCE_FRESHNESS_DAYS + 1
            status = "cached_fallback" if 0 <= age_days <= SOURCE_FRESHNESS_DAYS else "stale"
            full_text = cached.get("full_text", "")
            record = source | {
                "source_id": source_id,
                "retrieval_status": status,
                "retrieved_at": retrieved_at,
                "content_sha256": cached.get("content_sha256"),
                "source_error": type(exc).__name__,
                "text_excerpt": _source_excerpt(source | {"source_id": source_id}, full_text),
            }
        else:
            record = source | {
                "source_id": source_id,
                "retrieval_status": "stale",
                "retrieved_at": None,
                "content_sha256": None,
                "source_error": type(exc).__name__,
                "text_excerpt": "",
            }
    LEGAL_SOURCE_RUNTIME_CACHE[source_id] = record
    return record

def step3_lookup_legal_sources(step1_out: Dict, payee_country: str) -> Dict:
    domestic = step1_out["substantive_law"]
    forms = step1_out["procedural_forms"]
    treaty = TREATY_INDEX.get(payee_country, TREATY_INDEX["NONE"])
    source_ids = list(dict.fromkeys(domestic["source_ids"] + forms["source_ids"] + treaty["source_ids"]))
    records = [_get_legal_source(source_id) for source_id in source_ids]
    stale_ids = [record["source_id"] for record in records if record["retrieval_status"] == "stale"]
    return {
        "domestic": domestic,
        "forms": forms,
        "treaty": treaty,
        "source_records": records,
        "stale_source_ids": stale_ids,
    }

_REF = re.compile(r"(section|sec\.?|article|art\.?|rule)\s*(\d+[a-z]?)((?:\s*\(\s*[a-z0-9]+\s*\))?)", re.I)

def _kind(word: str) -> str:
    w = word.lower().rstrip(".")
    return {"sec": "section", "art": "article"}.get(w, w)

def _ref_key(kind: str, number: str, paren: str) -> str:
    paren = re.sub(r"\s+", "", paren)
    return (kind + number + paren).lower()

def check_cited_references(cited: str, retrieved: dict):
    """Check that each cited section/article is listed in the source registry, not that it supports a claim."""
    wanted = [_ref_key(_kind(k), n, p) for k, n, p in _REF.findall(cited or "")]
    if not wanted:
        return False, ["no citation reference supplied"]
    refs = list(retrieved["domestic"].get("citation_refs", []))
    refs += retrieved["forms"].get("citation_refs", [])
    refs += retrieved["treaty"].get("citation_refs", [])
    available = {_ref_key(_kind(k), n, p) for ref in refs for k, n, p in _REF.findall(ref)}
    available |= {re.sub(r"\([^)]*\)", "", ref) for ref in available}
    missing = []
    for wanted_ref in wanted:
        root_ref = re.sub(r"\([^)]*\)", "", wanted_ref)
        if wanted_ref not in available and root_ref not in available:
            missing.append(wanted_ref)
    return (len(missing) == 0), missing

def check_citation_roots_in_source_text(cited: str, retrieved: dict):
    """Check citation root labels appear in retrieved text; this does not validate claim support."""
    found = [_REF.findall(cited or "")]
    references = [(kind, number) for group in found for kind, number, _ in group]
    if not references:
        return False, ["no citation reference supplied"]
    source_ids = [record.get("source_id") for record in retrieved.get("source_records", [])]
    texts = [LEGAL_SOURCE_TEXT_RUNTIME_CACHE.get(source_id, "") for source_id in source_ids]
    combined_text = " ".join(texts).casefold()
    missing = []
    for kind, number in references:
        root_kind = _kind(kind)
        label = "art(?:icle)?" if root_kind == "article" else ("sec(?:tion)?" if root_kind == "section" else "rule")
        # Official Income-tax pages often render headings as "Rule - 220".
        # Permit a dash between a citation label and its number while keeping
        # this metric limited to root-label presence, not citation validation.
        pattern = rf"\b{label}\s*(?:[-–—]\s*)?\.?\s*{re.escape(number.casefold())}\b"
        if not re.search(pattern, combined_text, flags=re.I):
            missing.append(f"{root_kind} {number}")
    return (len(missing) == 0), missing
