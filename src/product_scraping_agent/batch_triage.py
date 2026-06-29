"""Post-process batch output into a triage CSV and summary JSON."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .business_validation import build_business_validation_report_from_row
from .image_diagnostics import build_image_diagnostics_from_row
from .page_classification import classify_page_from_row

TRIAGE_COLUMNS = [
    "triage_bucket",
    "triage_priority",
    "page_classification_status",
    "page_classification_confidence",
    "page_classification_reasons",
    "image_failure_bucket",
    "image_recovery_action",
    "image_error_buckets",
    "image_attempt_buckets",
    "image_manifest_item_count",
    "screenshot_fallback_file_detected",
]


def _priority(bucket: str) -> int:
    order = {
        "FAILED_TECHNICAL": 100,
        "REVIEW_BLOCKED_ACCESS": 90,
        "REVIEW_IDENTITY_CONFLICT": 85,
        "REVIEW_NOT_PRODUCT_DETAIL_PAGE": 80,
        "REVIEW_IMAGE_FAILED": 75,
        "REVIEW_IMAGE_ONLY": 70,
        "REVIEW_WEAK_CAPTURE": 60,
        "REVIEW_SOURCE_FALLBACK": 50,
        "REVIEW_MISSING_CRITICAL_FIELDS": 45,
        "REVIEW_REQUIRED_BY_QUALITY_GATE": 40,
        "READY_FOR_CODING": 0,
    }
    return order.get(bucket or "", 30)


def enrich_row_for_triage(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    business = build_business_validation_report_from_row(row)
    page = classify_page_from_row(row)
    image = build_image_diagnostics_from_row(row)
    out.update(business)
    out.update(page.as_dict())
    out.update(image)
    bucket = str(out.get("manual_review_bucket") or "REVIEW_REQUIRED_BY_QUALITY_GATE")
    out["triage_bucket"] = bucket
    out["triage_priority"] = _priority(bucket)
    return out


def triage_batch_output_csv(input_csv: Path, *, output_csv: Path, summary_json: Path | None = None) -> dict[str, Any]:
    with Path(input_csv).open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = [enrich_row_for_triage(dict(row)) for row in reader]
        fieldnames = list(reader.fieldnames or [])
    if not fieldnames:
        raise ValueError(f"No CSV header found: {input_csv}")
    for row in rows:
        for col in list(row.keys()):
            if col not in fieldnames:
                fieldnames.append(col)
    rows.sort(key=lambda r: int(float(str(r.get("triage_priority") or 0))), reverse=True)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    bucket_counts = Counter(str(r.get("triage_bucket") or "unknown") for r in rows)
    visual_counts = Counter(str(r.get("visual_evidence_status") or "unknown") for r in rows)
    page_counts = Counter(str(r.get("page_classification_status") or "unknown") for r in rows)
    image_buckets = Counter(str(r.get("image_failure_bucket") or "unknown") for r in rows)
    summary = {
        "input_csv": str(input_csv),
        "output_csv": str(output_csv),
        "total_rows": len(rows),
        "ready_for_coding": bucket_counts.get("READY_FOR_CODING", 0),
        "needs_review": len(rows) - bucket_counts.get("READY_FOR_CODING", 0),
        "triage_bucket_counts": dict(bucket_counts),
        "visual_evidence_status_counts": dict(visual_counts),
        "page_classification_counts": dict(page_counts),
        "image_failure_bucket_counts": dict(image_buckets),
    }
    if summary_json:
        summary_json.parent.mkdir(parents=True, exist_ok=True)
        summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


__all__ = ["TRIAGE_COLUMNS", "enrich_row_for_triage", "triage_batch_output_csv"]
