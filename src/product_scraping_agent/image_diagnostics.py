"""Image recovery diagnostics for batch and audit reporting."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


def _read_json(path_value: Any) -> Any:
    text = str(path_value or "").strip()
    if not text:
        return None
    try:
        path = Path(text)
        if path.exists() and path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return None


def _iter_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("images", "items", "image_refs", "candidates"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def _bucket_error(error: str) -> str:
    e = (error or "").lower()
    if not e:
        return "no_error"
    if "403" in e or "forbidden" in e:
        return "image_fetch_forbidden"
    if "401" in e or "unauthorized" in e:
        return "image_fetch_needs_session"
    if "404" in e or "not found" in e:
        return "image_not_found"
    if "429" in e or "rate" in e:
        return "rate_limited"
    if "timeout" in e:
        return "timeout"
    if "non-image" in e or "mime=" in e or "html" in e:
        return "non_image_payload"
    if "invalid image" in e:
        return "invalid_image_payload"
    if "too large" in e:
        return "image_too_large"
    if "svg" in e or "vector" in e:
        return "svg_or_vector_excluded"
    if "unrelated" in e or "vision verdict" in e:
        return "vision_rejected_not_product"
    if "unverified" in e:
        return "vision_unverified"
    if "thumbnail" in e:
        return "thumbnail_generation_failed"
    if "download failed" in e or "connect" in e:
        return "download_failed"
    return "other_image_error"


def _safe_int(value: Any) -> int:
    try:
        return int(float(str(value or "").strip()))
    except Exception:
        return 0


def build_image_diagnostics_from_row(row: dict[str, Any]) -> dict[str, Any]:
    manifest = _read_json(row.get("image_manifest_path"))
    items = _iter_items(manifest)
    errors = Counter()
    attempts = Counter()
    local_files = 0
    screenshot_file = False
    for item in items:
        local_path = str(item.get("local_path") or item.get("path") or "")
        if local_path:
            local_files += 1
            if "screenshot_fallback" in local_path:
                screenshot_file = True
        error = str(item.get("error") or "")
        if error:
            errors[_bucket_error(error)] += 1
        for attempt in item.get("download_attempts") or []:
            if isinstance(attempt, dict):
                attempts[f"{attempt.get('strategy') or 'unknown'}:{attempt.get('status') or ''}"] += 1

    visual_status = str(row.get("visual_evidence_status") or "")
    final_count = _safe_int(row.get("final_image_count"))
    candidate_count = _safe_int(row.get("image_candidate_count"))
    downloaded_count = _safe_int(row.get("image_downloaded_count")) or local_files
    screenshot_used = str(row.get("screenshot_fallback_used") or "").lower() in {"1", "true", "yes"}

    if visual_status == "final_product_images_available" and final_count >= 1:
        bucket = "image_ready"
        action = "ready"
    elif visual_status == "screenshot_fallback_only" or screenshot_file or screenshot_used:
        bucket = "screenshot_fallback_only"
        action = "manual_review_screenshot_or_retry_image_capture"
    elif candidate_count == 0:
        bucket = "no_image_candidates"
        action = "inspect_page_capture_or_gallery_extraction"
    elif downloaded_count == 0:
        bucket = errors.most_common(1)[0][0] if errors else "all_image_downloads_failed"
        action = "retry_image_capture_with_stronger_profile"
    elif final_count == 0:
        bucket = errors.most_common(1)[0][0] if errors else "downloaded_but_no_final_product_image"
        action = "review_vision_filtering_or_product_image_selection"
    else:
        bucket = "image_partial_or_unverified"
        action = "manual_review_images"

    return {
        "image_failure_bucket": bucket,
        "image_recovery_action": action,
        "image_error_buckets": "; ".join(f"{k}:{v}" for k, v in errors.most_common()),
        "image_attempt_buckets": "; ".join(f"{k}:{v}" for k, v in attempts.most_common(8)),
        "image_manifest_item_count": len(items),
        "screenshot_fallback_file_detected": screenshot_file,
    }


__all__ = ["build_image_diagnostics_from_row"]
