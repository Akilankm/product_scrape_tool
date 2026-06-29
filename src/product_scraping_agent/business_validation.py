"""Deterministic business-readiness validation for batch scrape output.

This module does not scrape, search, or add facts. It classifies whether an
already-created product evidence artifact is safe for downstream automated
product coding.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

from .text_utils import digits_only

BUSINESS_VALIDATION_OUTPUT_COLUMNS: list[str] = [
    "technical_success",
    "scrape_success",
    "evidence_success",
    "visual_success",
    "business_validation_status",
    "business_validation_score",
    "manual_review_bucket",
    "manual_review_reason",
    "identity_match_status",
    "identity_match_score",
    "identity_match_terms",
    "ean_match_status",
    "ean_conflict_candidates",
    "variant_match_status",
    "is_product_detail_page",
    "is_category_or_search_page",
    "is_marketplace_page",
    "main_product_isolation_status",
    "critical_conflicts",
]

_BLOCKED_ACCESS = {"bot_challenge", "access_denied", "geo_restricted", "rate_limited", "fetch_error"}
_REVIEW_DECISIONS = {
    "mixed_capture_needs_review",
    "blocked_shell_capture",
    "empty_or_blocked_capture",
    "weak_no_real_product_capture",
    "blocked_or_challenge_capture",
    "input_url_only_artifact",
    "fetch_failed_input_url_only_artifact",
    "worker_failed_finalized_artifact",
}
_STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "product", "item",
    "toy", "toys", "set", "pack", "piece", "pieces", "unit", "units", "new",
    "official", "original", "retail", "retailer", "online", "shop",
}
_PRODUCT_TERMS = {
    "brand", "manufacturer", "ean", "gtin", "sku", "mpn", "model", "description",
    "specification", "details", "features", "age", "material", "dimensions",
    "contents", "warning", "price", "availability",
}
_MARKETPLACE_TERMS = (
    "sold by", "seller", "marketplace", "fulfilled by", "ships from", "third-party",
    "third party", "vendor", "merchant",
)
_CATEGORY_PATTERNS = (
    re.compile(r"(?:^|[/_.-])(search|category|categories|catalog|collection|collections|listing|results|plp)(?:[/_.-]|$)", re.I),
    re.compile(r"[?&](?:q|query|search|keyword|s)=", re.I),
)


def _bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value or "").strip()))
    except Exception:
        return default


def _read(path_value: Any, max_chars: int = 120_000) -> str:
    text = str(path_value or "").strip()
    if not text:
        return ""
    try:
        path = Path(text)
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except Exception:
        return ""
    return ""


def _tokens(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"[^A-Za-z0-9]+", (text or "").lower()):
        if len(token) < 3 or token in _STOPWORDS:
            continue
        if token.isdigit() and len(token) < 4:
            continue
        if token not in seen:
            seen.add(token)
            out.append(token)
    return out


def _identity_status(main_text: str, ean_status: str, blob: str) -> tuple[str, float, list[str]]:
    if ean_status == "matched":
        return "matched_by_ean", 1.0, []
    toks = _tokens(main_text)[:32]
    if not toks:
        return "no_input_identity", 0.0, []
    hits = [t for t in toks if t in blob]
    ratio = len(hits) / max(1, len(toks))
    if len(hits) >= 4 and ratio >= 0.65:
        return "matched_by_title_tokens", ratio, hits
    if len(hits) >= 3 and ratio >= 0.45:
        return "weak_match_by_title_tokens", ratio, hits
    if hits:
        return "partial_token_overlap", ratio, hits
    return "input_not_found_in_capture", ratio, hits


def _ean_status(ean: str, blob: str) -> tuple[str, list[str]]:
    clean = digits_only(ean or "")
    if not clean:
        return "not_provided", []
    if clean in digits_only(blob):
        return "matched", [clean]
    found = sorted(set(re.findall(r"(?<!\d)(?:\d{8}|\d{12}|\d{13}|\d{14})(?!\d)", blob)))
    found = [x for x in found if x != clean]
    if found:
        return "possible_identifier_conflict", found[:8]
    return "not_found_in_source", []


def _source_alignment_status(row: dict[str, Any]) -> str:
    explicit = str(row.get("source_alignment_status") or "").strip()
    if explicit:
        return explicit
    role = str(row.get("source_url_role") or "unknown").strip().lower().replace("-", "_").replace(" ", "_")
    if role in {"primary", "primary_requested_retailer", "requested_retailer", "same_retailer_same_country"}:
        return "primary_requested_source"
    if role in {"fallback", "marketplace_fallback", "global_fallback", "alternate_retailer_same_country", "alternate_retailer_different_country", "same_retailer_different_country", "different_retailer", "alternate_source"}:
        return "fallback_source_used"
    return "not_declared"


def _blob(row: dict[str, Any]) -> str:
    parts = [
        row.get("product_url", ""), row.get("final_url", ""), row.get("title", ""),
        row.get("weak_capture_reasons", ""), row.get("missing_critical_fields", ""),
        row.get("quality_warnings", ""), _read(row.get("product_evidence_json_path")),
        _read(row.get("product_evidence_md_path")), _read(row.get("claims_md_path")),
        _read(row.get("source_md_path")), _read(row.get("vision_md_path")),
        _read(row.get("metadata_json_path")),
    ]
    return "\n".join(str(p) for p in parts if p).lower()


def build_business_validation_report_from_row(row: dict[str, Any]) -> dict[str, Any]:
    blob = _blob(row)
    ean_status, ean_candidates = _ean_status(str(row.get("ean") or ""), blob)
    identity_status, identity_score, identity_terms = _identity_status(str(row.get("main_text") or ""), ean_status, blob)

    url_title = "\n".join(str(row.get(k) or "") for k in ("product_url", "final_url", "title"))
    category_or_search = any(p.search(url_title) for p in _CATEGORY_PATTERNS)
    if category_or_search and _bool(row.get("real_scrape_evidence")) and sum(1 for t in _PRODUCT_TERMS if t in blob) >= 4:
        category_or_search = False
    category_or_search = category_or_search or (not _bool(row.get("real_scrape_evidence")) and any(t in blob[:30_000] for t in ("search results", "category page", "product listing", "showing results")))

    capture_decision = str(row.get("capture_decision") or "not_evaluated")
    real_scrape = _bool(row.get("real_scrape_evidence"))
    product_detail = bool(not category_or_search and (capture_decision in {"rich_product_capture", "usable_product_capture"} and real_scrape or real_scrape and sum(1 for t in _PRODUCT_TERMS if t in blob) >= 4))
    marketplace = any(t in blob[:80_000] for t in _MARKETPLACE_TERMS)

    technical_success = _bool(row.get("success")) and not str(row.get("error") or "").strip()
    scrape_success = real_scrape and capture_decision in {"rich_product_capture", "usable_product_capture", "mixed_capture_needs_review"}
    visual_success = str(row.get("visual_evidence_status") or "") == "final_product_images_available" and _int(row.get("final_image_count")) >= 1
    missing = [x.strip() for x in str(row.get("missing_critical_fields") or "").split(";") if x.strip()]
    artifact_quality = str(row.get("artifact_quality") or "")
    evidence_success = artifact_quality in {"strong", "usable"} and not missing and product_detail and identity_status in {"matched_by_ean", "matched_by_title_tokens", "weak_match_by_title_tokens", "no_input_identity"}

    variant_status = "not_evaluated"
    if identity_status in {"matched_by_ean", "matched_by_title_tokens"}:
        variant_status = "aligned_or_not_detected"
    elif identity_status in {"weak_match_by_title_tokens", "partial_token_overlap"}:
        variant_status = "possible_variant_risk"
    elif identity_status == "input_not_found_in_capture":
        variant_status = "possible_variant_or_wrong_product"

    source_status = _source_alignment_status(row)
    conflicts: list[str] = []
    if ean_status == "possible_identifier_conflict":
        conflicts.append("ean_or_identifier_conflict")
    if identity_status == "input_not_found_in_capture":
        conflicts.append("input_identity_not_found_in_capture")
    if category_or_search:
        conflicts.append("category_or_search_page_not_product_detail_page")
    if not visual_success:
        conflicts.append("clean_product_image_missing")
    if str(row.get("access_status") or "") in _BLOCKED_ACCESS:
        conflicts.append(f"access_status_{row.get('access_status')}")
    if source_status in {"fallback_source_used", "source_context_mismatch"}:
        conflicts.append(source_status)

    if not technical_success:
        bucket = "FAILED_TECHNICAL"
    elif str(row.get("access_status") or "") in _BLOCKED_ACCESS or capture_decision in {"blocked_shell_capture", "blocked_or_challenge_capture"}:
        bucket = "REVIEW_BLOCKED_ACCESS"
    elif ean_status == "possible_identifier_conflict" or identity_status == "input_not_found_in_capture":
        bucket = "REVIEW_IDENTITY_CONFLICT"
    elif category_or_search or not product_detail:
        bucket = "REVIEW_NOT_PRODUCT_DETAIL_PAGE"
    elif not visual_success:
        bucket = "REVIEW_IMAGE_ONLY" if str(row.get("visual_evidence_status") or "") == "screenshot_fallback_only" else "REVIEW_IMAGE_FAILED"
    elif source_status in {"fallback_source_used", "source_context_mismatch"}:
        bucket = "REVIEW_SOURCE_FALLBACK"
    elif str(row.get("capture_grade") or "") in {"weak", "blocked_or_shell", "mixed_capture"} or capture_decision in _REVIEW_DECISIONS:
        bucket = "REVIEW_WEAK_CAPTURE"
    elif missing or not evidence_success:
        bucket = "REVIEW_MISSING_CRITICAL_FIELDS"
    elif _bool(row.get("requires_manual_review")):
        bucket = "REVIEW_REQUIRED_BY_QUALITY_GATE"
    else:
        bucket = "READY_FOR_CODING"

    score = 100
    if not technical_success: score -= 60
    if not scrape_success: score -= 25
    if not evidence_success: score -= 20
    if not visual_success: score -= 30
    if identity_status == "input_not_found_in_capture": score -= 35
    elif identity_status in {"partial_token_overlap", "weak_match_by_title_tokens"}: score -= 10
    if ean_status == "possible_identifier_conflict": score -= 40
    if category_or_search: score -= 35
    if source_status in {"fallback_source_used", "source_context_mismatch"}: score -= 10
    score = max(0, min(100, score))

    isolation = "isolated_main_product" if product_detail and not category_or_search else "not_isolated_or_not_product_page"
    if str(row.get("visual_evidence_status") or "") == "screenshot_fallback_only":
        isolation = "screenshot_needs_main_product_review"
    elif marketplace:
        isolation = "marketplace_page_needs_seller_scope_review"

    return {
        "technical_success": technical_success,
        "scrape_success": scrape_success,
        "evidence_success": evidence_success,
        "visual_success": visual_success,
        "business_validation_status": "ready" if bucket == "READY_FOR_CODING" else "failed" if bucket == "FAILED_TECHNICAL" else "review",
        "business_validation_score": score,
        "manual_review_bucket": bucket,
        "manual_review_reason": "; ".join(dict.fromkeys(conflicts or [bucket])),
        "identity_match_status": identity_status,
        "identity_match_score": round(identity_score, 3),
        "identity_match_terms": "; ".join(identity_terms),
        "ean_match_status": ean_status,
        "ean_conflict_candidates": "; ".join(ean_candidates),
        "variant_match_status": variant_status,
        "is_product_detail_page": product_detail,
        "is_category_or_search_page": category_or_search,
        "is_marketplace_page": marketplace,
        "main_product_isolation_status": isolation,
        "critical_conflicts": "; ".join(dict.fromkeys(conflicts)),
    }


def enrich_batch_output_csv(path: Path) -> None:
    """Append business-validation columns to an existing batch output CSV."""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = [dict(r) for r in reader]
        fieldnames = list(reader.fieldnames or [])
    if not fieldnames:
        return
    for col in BUSINESS_VALIDATION_OUTPUT_COLUMNS:
        if col not in fieldnames:
            fieldnames.append(col)
    for row in rows:
        row.update(build_business_validation_report_from_row(row))
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


__all__ = ["BUSINESS_VALIDATION_OUTPUT_COLUMNS", "build_business_validation_report_from_row", "enrich_batch_output_csv"]
