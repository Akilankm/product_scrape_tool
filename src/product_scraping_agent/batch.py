"""Batch CSV runner for URL-in/product-artifact-out scraping.

This module intentionally contains no search/discovery logic. Each input row must
already contain the URL that should be scraped. The batch runner maps every input
row to exactly one artifact folder and writes an output CSV that downstream
systems can consume.
"""

from __future__ import annotations

import asyncio
import csv
import json
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Iterable

from .agent import ProductScrapingAgent
from .models import EvidenceSourceItem, ScrapeRequest, ScrapeResult


DEFAULT_BATCH_OUTPUT_COLUMNS: list[str] = [
    "row_number",
    "input_id",
    "product_url",
    "main_text",
    "ean",
    "requested_retailer_name",
    "requested_country_code",
    "source_retailer_name",
    "source_country_code",
    "source_url_role",
    "success",
    "artifact_quality",
    "quality_score",
    "requires_manual_review",
    "missing_critical_fields",
    "quality_warnings",
    "access_status",
    "access_issue_type",
    "browser_visible",
    "product_details_recovered",
    "recovery_status",
    "evidence_axes_used",
    "capture_profile_used",
    "capture_profiles_attempted",
    "capture_score",
    "capture_grade",
    "capture_decision",
    "real_scrape_evidence",
    "weak_capture_reasons",
    "is_weak_capture",
    "is_block_or_challenge",
    "has_real_scrape_evidence",
    "capture_decision_bucket",
    "source_alignment_status",
    "source_claim_scope",
    "requested_retailer_claims_allowed",
    "final_url",
    "title",
    "artifact_dir",
    "request_json_path",
    "scrape_result_json_path",
    "product_evidence_json_path",
    "product_evidence_md_path",
    "claims_md_path",
    "source_md_path",
    "vision_md_path",
    "quality_report_path",
    "source_alignment_report_path",
    "evidence_recovery_report_path",
    "metadata_json_path",
    "image_manifest_path",
    "table_manifest_path",
    "artifact_manifest_path",
    "agent_trace_path",
    "image_candidate_count",
    "final_image_count",
    "image_downloaded_count",
    "vision_described_count",
    "table_count",
    "json_ld_count",
    "elapsed_seconds",
    "error",
]

_URL_COLUMNS = ("product_url", "url", "PRODUCT_URL", "URL")
_INPUT_ID_COLUMNS = ("input_id", "id", "row_id", "product_id", "serial_id", "SERIAL_ID")


def _first(row: dict[str, Any], *names: str, default: str = "") -> str:
    """Return the first non-empty value from a row using case-sensitive/case-insensitive keys."""
    for name in names:
        if name in row and row[name] not in (None, ""):
            return str(row[name]).strip()
    lower_map = {str(k).lower(): v for k, v in row.items()}
    for name in names:
        value = lower_map.get(name.lower())
        if value not in (None, ""):
            return str(value).strip()
    return default


def stable_scrape_id(value: str, row_number: int) -> str:
    """Make a stable filesystem-safe scrape id from input_id.

    Stable IDs are important for resumable batch runs. If no id is supplied, a
    deterministic row id is generated.
    """
    raw = (value or f"row_{row_number:06d}").strip()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-._")
    return safe or f"row_{row_number:06d}"


def _truthy(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _int_or_default(value: Any, default: int) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(float(str(value).strip()))
    except ValueError:
        return default


def _split_snippets(value: str) -> list[str]:
    text = (value or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            pass
    # Support multiline cells and simple pipe-separated exports.
    pieces = re.split(r"\n+|\|\|", text)
    return [p.strip() for p in pieces if p.strip()]


def _load_text_or_inline(value: str, *, base_dir: Path | None = None) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    path = Path(text)
    candidates = [path]
    if base_dir is not None and not path.is_absolute():
        candidates.insert(0, base_dir / path)
    for candidate in candidates:
        try:
            if candidate.exists() and candidate.is_file():
                return candidate.read_text(encoding="utf-8")
        except OSError:
            continue
    return text


def _parse_search_evidence(value: str, *, base_dir: Path | None = None) -> list[EvidenceSourceItem]:
    text = (value or "").strip()
    if not text:
        return []
    payload = _load_text_or_inline(text, base_dir=base_dir)
    try:
        data = json.loads(payload)
    except Exception:
        return [EvidenceSourceItem(source_type="batch_cell", text=payload)] if payload else []
    if isinstance(data, dict) and "items" in data:
        data = data["items"]
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return [EvidenceSourceItem(source_type="batch_json", raw={"value": data})]
    out: list[EvidenceSourceItem] = []
    for item in data:
        if isinstance(item, dict):
            out.append(EvidenceSourceItem.model_validate(item))
        else:
            out.append(EvidenceSourceItem(source_type="batch_json", text=str(item)))
    return out


def read_input_csv(path: Path) -> list[dict[str, str]]:
    """Read a UTF-8/UTF-8-SIG CSV into row dictionaries."""
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"Input CSV has no header row: {path}")
        return [dict(row) for row in reader]


@dataclass(frozen=True)
class BatchOptions:
    output_root: Path
    retailer_label: str = "retailer"
    max_concurrency: int = 2
    max_images: int = 30
    vision_max: int = 12
    max_agent_iterations: int = 2
    write_raw_debug: bool | None = None
    resume: bool = False
    skip_existing_artifacts: bool = False
    stop_on_error: bool = False
    domain_profile_learning: bool = True


@dataclass(frozen=True)
class BatchSummary:
    input_csv: Path
    output_csv: Path
    output_root: Path
    total_rows: int
    processed_rows: int
    skipped_rows: int
    success_rows: int
    failed_rows: int
    manual_review_rows: int
    elapsed_seconds: float
    quality_counts: dict[str, int]
    domain_profile_preferences: dict[str, str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_csv": str(self.input_csv),
            "output_csv": str(self.output_csv),
            "output_root": str(self.output_root),
            "total_rows": self.total_rows,
            "processed_rows": self.processed_rows,
            "skipped_rows": self.skipped_rows,
            "success_rows": self.success_rows,
            "failed_rows": self.failed_rows,
            "manual_review_rows": self.manual_review_rows,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "quality_counts": dict(self.quality_counts),
            "domain_profile_preferences": dict(self.domain_profile_preferences or {}),
        }


def request_from_csv_row(
    row: dict[str, Any],
    *,
    row_number: int,
    output_root: Path,
    default_retailer_label: str = "retailer",
    default_max_images: int = 30,
    default_vision_max: int = 12,
    default_max_agent_iterations: int = 2,
    default_write_raw_debug: bool | None = None,
    base_dir: Path | None = None,
) -> ScrapeRequest:
    """Convert one CSV row into a ScrapeRequest.

    The parser intentionally accepts common aliases so that output from upstream
    URL discovery/search systems can be passed directly with minimal reshaping.
    """
    product_url = _first(row, *_URL_COLUMNS)
    if not product_url:
        raise ValueError("Missing required product_url/url column value")

    input_id = _first(row, *_INPUT_ID_COLUMNS)
    scrape_id = _first(row, "scrape_id") or stable_scrape_id(input_id, row_number)

    requested_retailer = _first(row, "requested_retailer_name", "requested_retailer", "target_retailer_name")
    requested_country = _first(row, "requested_country_code", "requested_country", "target_country_code")
    # Backward-compatible aliases represent requested context when explicit
    # requested_* columns are absent.
    retailer_name = _first(row, "retailer_name", "retailer", "RETAILER")
    country_code = _first(row, "country_code", "country", "COUNTRY")
    if not requested_retailer:
        requested_retailer = retailer_name
    if not requested_country:
        requested_country = country_code

    raw_debug = _truthy(_first(row, "write_raw_debug"))
    if raw_debug is None:
        raw_debug = default_write_raw_debug

    ai_inline = _first(row, "upstream_ai_evidence", "ai_mode_evidence")
    ai_file = _first(row, "upstream_ai_evidence_file", "ai_mode_evidence_file")
    ai_parts = []
    if ai_inline:
        ai_parts.append(_load_text_or_inline(ai_inline, base_dir=base_dir))
    if ai_file:
        ai_parts.append(_load_text_or_inline(ai_file, base_dir=base_dir))

    candidate_snippets = _split_snippets(_first(row, "candidate_snippets", "candidate_snippet", "snippets"))
    search_evidence = _parse_search_evidence(
        _first(row, "search_evidence", "search_evidence_json", "upstream_search_evidence"),
        base_dir=base_dir,
    )

    return ScrapeRequest(
        product_url=product_url,
        scrape_id=scrape_id,
        main_text=_first(row, "main_text", "MAIN_TEXT", "product_text", "product_name", "title"),
        ean=_first(row, "ean", "EAN", "gtin", "GTIN"),
        retailer_name=retailer_name,
        country_code=country_code,
        requested_retailer_name=requested_retailer,
        requested_country_code=requested_country,
        source_retailer_name=_first(row, "source_retailer_name", "source_retailer", "actual_retailer_name"),
        source_country_code=_first(row, "source_country_code", "source_country", "actual_country_code"),
        source_url_role=_first(row, "source_url_role", "url_role", default="unknown"),
        product_hint=_first(row, "product_hint"),
        upstream_ai_evidence="\n\n".join(p.strip() for p in ai_parts if p and p.strip()),
        candidate_snippets=candidate_snippets,
        search_evidence=search_evidence,
        upstream_evidence_notes=_first(row, "upstream_evidence_notes", "evidence_notes"),
        output_root=output_root,
        retailer_label=_first(row, "retailer_label", default=default_retailer_label) or default_retailer_label,
        max_images=_int_or_default(_first(row, "max_images"), default_max_images),
        vision_max=_int_or_default(_first(row, "vision_max"), default_vision_max),
        max_agent_iterations=_int_or_default(_first(row, "max_agent_iterations"), default_max_agent_iterations),
        write_raw_debug=raw_debug,
    )


def _path_str(path: Path | None) -> str:
    return str(path) if path else ""


def _capture_bucket(result: ScrapeResult | None) -> str:
    if result is None:
        return "not_created"
    decision = getattr(result, "capture_decision", "") or ""
    if decision in {"rich_product_capture", "usable_product_capture"}:
        return "real_product_capture"
    if decision == "mixed_capture_needs_review":
        return "mixed_needs_review"
    if decision in {"blocked_or_challenge_capture", "blocked_shell_capture"}:
        return "blocked_or_challenge"
    if decision in {"input_url_only_artifact", "fetch_failed_input_url_only_artifact"}:
        return "input_url_only"
    if decision:
        return decision
    if getattr(result, "real_scrape_evidence", False):
        return "real_product_capture"
    if getattr(result, "access_status", "") in {"bot_challenge", "access_denied", "fetch_error"}:
        return "blocked_or_challenge"
    return "weak_or_unknown"


def result_to_output_row(
    *,
    row_number: int,
    input_id: str,
    request: ScrapeRequest,
    result: ScrapeResult | None = None,
    error: str = "",
) -> dict[str, Any]:
    """Flatten a ScrapeResult into one batch mapping CSV row."""
    if result is None:
        return {
            "row_number": row_number,
            "input_id": input_id,
            "product_url": request.product_url,
            "main_text": request.main_text,
            "ean": request.ean,
            "requested_retailer_name": request.source_alignment.requested_retailer_name,
            "requested_country_code": request.source_alignment.requested_country_code,
            "source_retailer_name": request.source_alignment.source_retailer_name,
            "source_country_code": request.source_alignment.source_country_code,
            "source_url_role": request.source_alignment.source_url_role,
            "success": False,
            "artifact_quality": "not_created",
            "quality_score": 0,
            "requires_manual_review": True,
            "missing_critical_fields": "",
            "quality_warnings": "",
            "access_status": "not_attempted",
            "access_issue_type": "",
            "browser_visible": False,
            "product_details_recovered": False,
            "recovery_status": "not_attempted",
            "evidence_axes_used": "",
            "capture_profile_used": "",
            "capture_profiles_attempted": "",
            "capture_score": 0,
            "capture_grade": "not_evaluated",
            "capture_decision": "not_evaluated",
            "real_scrape_evidence": False,
            "weak_capture_reasons": "",
            "is_weak_capture": True,
            "is_block_or_challenge": False,
            "has_real_scrape_evidence": False,
            "capture_decision_bucket": "not_created",
            "source_alignment_status": request.source_alignment.alignment_status,
            "source_claim_scope": request.source_alignment.source_specific_claim_scope,
            "requested_retailer_claims_allowed": request.source_alignment.requested_retailer_claims_allowed,
            "final_url": "",
            "title": "",
            "artifact_dir": "",
            "request_json_path": "",
            "scrape_result_json_path": "",
            "product_evidence_json_path": "",
            "product_evidence_md_path": "",
            "claims_md_path": "",
            "source_md_path": "",
            "vision_md_path": "",
            "quality_report_path": "",
            "source_alignment_report_path": "",
            "evidence_recovery_report_path": "",
            "metadata_json_path": "",
            "image_manifest_path": "",
            "table_manifest_path": "",
            "artifact_manifest_path": "",
            "agent_trace_path": "",
            "image_candidate_count": 0,
            "final_image_count": 0,
            "image_downloaded_count": 0,
            "vision_described_count": 0,
            "table_count": 0,
            "json_ld_count": 0,
            "elapsed_seconds": 0,
            "error": error,
        }

    return {
        "row_number": row_number,
        "input_id": input_id,
        "product_url": result.product_url,
        "main_text": request.main_text,
        "ean": request.ean,
        "requested_retailer_name": result.source_alignment.requested_retailer_name,
        "requested_country_code": result.source_alignment.requested_country_code,
        "source_retailer_name": result.source_alignment.source_retailer_name,
        "source_country_code": result.source_alignment.source_country_code,
        "source_url_role": result.source_alignment.source_url_role,
        "success": result.success,
        "artifact_quality": result.artifact_quality,
        "quality_score": result.quality_score,
        "requires_manual_review": result.requires_manual_review,
        "missing_critical_fields": "; ".join(result.missing_critical_fields),
        "quality_warnings": "; ".join(result.quality_warnings),
        "access_status": result.access_status,
        "access_issue_type": result.access_issue_type,
        "browser_visible": result.browser_visible,
        "product_details_recovered": result.product_details_recovered,
        "recovery_status": result.recovery_status,
        "evidence_axes_used": "; ".join(result.evidence_axes_used),
        "capture_profile_used": result.capture_profile_used,
        "capture_profiles_attempted": "; ".join(result.capture_profiles_attempted),
        "capture_score": result.capture_score,
        "capture_grade": result.capture_grade,
        "capture_decision": getattr(result, "capture_decision", "not_evaluated"),
        "real_scrape_evidence": result.real_scrape_evidence,
        "weak_capture_reasons": "; ".join(result.weak_capture_reasons),
        "is_weak_capture": bool(result.capture_grade in {"weak", "blocked_or_shell", "mixed_capture"} or getattr(result, "capture_decision", "") in {"blocked_shell_capture", "empty_or_blocked_capture", "weak_no_real_product_capture", "mixed_capture_needs_review"}),
        "is_block_or_challenge": bool(result.access_status in {"bot_challenge", "access_denied", "geo_restricted", "rate_limited"} or "block" in (getattr(result, "capture_decision", "") or "") or any("block" in str(x).lower() or "challenge" in str(x).lower() for x in result.weak_capture_reasons)),
        "has_real_scrape_evidence": bool(result.real_scrape_evidence),
        "capture_decision_bucket": ("rich" if getattr(result, "capture_decision", "") == "rich_product_capture" else "usable" if getattr(result, "capture_decision", "") == "usable_product_capture" else "mixed_review" if getattr(result, "capture_decision", "") == "mixed_capture_needs_review" else "weak" if "weak" in (getattr(result, "capture_decision", "") or "") else "blocked" if "block" in (getattr(result, "capture_decision", "") or "") or "empty" in (getattr(result, "capture_decision", "") or "") else getattr(result, "capture_decision", "not_evaluated") or "unknown"),
        "source_alignment_status": result.source_alignment.alignment_status,
        "source_claim_scope": result.source_alignment.source_specific_claim_scope,
        "requested_retailer_claims_allowed": result.source_alignment.requested_retailer_claims_allowed,
        "final_url": result.final_url,
        "title": result.title,
        "artifact_dir": _path_str(result.output_dir),
        "request_json_path": _path_str(result.request_json_path),
        "scrape_result_json_path": _path_str(result.scrape_result_json_path),
        "product_evidence_json_path": _path_str(result.product_evidence_json_path),
        "product_evidence_md_path": _path_str(result.product_evidence_md_path),
        "claims_md_path": _path_str(result.claims_md_path),
        "source_md_path": _path_str(result.source_md_path),
        "vision_md_path": _path_str(result.vision_md_path),
        "quality_report_path": _path_str(result.quality_report_json_path),
        "source_alignment_report_path": _path_str(result.source_alignment_report_json_path),
        "evidence_recovery_report_path": _path_str(result.evidence_recovery_report_json_path),
        "metadata_json_path": _path_str(result.metadata_json_path),
        "image_manifest_path": _path_str(result.image_manifest_path),
        "table_manifest_path": _path_str(result.table_manifest_path),
        "artifact_manifest_path": _path_str(result.artifact_manifest_path),
        "agent_trace_path": _path_str(result.agent_trace_json_path),
        "image_candidate_count": result.image_candidate_count,
        "final_image_count": result.final_image_count,
        "image_downloaded_count": result.image_downloaded_count,
        "vision_described_count": result.vision_described_count,
        "table_count": result.table_count,
        "json_ld_count": result.json_ld_count,
        "elapsed_seconds": round(result.elapsed_seconds, 3),
        "error": error or result.error,
    }


def _existing_success_ids(output_csv: Path) -> set[str]:
    if not output_csv.exists():
        return set()
    try:
        with output_csv.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            return {
                str(row.get("input_id") or "").strip()
                for row in reader
                if str(row.get("success") or "").strip().lower() in {"true", "1", "yes"}
                and str(row.get("input_id") or "").strip()
            }
    except Exception:
        return set()


def _prepare_output_csv(path: Path, *, append: bool) -> tuple[Any, csv.DictWriter]:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    handle = path.open("a" if append else "w", encoding="utf-8", newline="")
    writer = csv.DictWriter(handle, fieldnames=DEFAULT_BATCH_OUTPUT_COLUMNS, extrasaction="ignore")
    if not append or not exists:
        writer.writeheader()
        handle.flush()
    return handle, writer



def _domain_from_url(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower().split('@')[-1].split(':')[0]
        return host[4:] if host.startswith('www.') else host
    except Exception:
        return ""


def _reordered_sequence(base_sequence: str, preferred_profile: str) -> str:
    profiles = [p.strip() for p in re.split(r"[,;\s]+", base_sequence or "") if p.strip()]
    if not preferred_profile:
        return ",".join(profiles) or base_sequence
    out = [preferred_profile]
    out.extend(p for p in profiles if p != preferred_profile)
    return ",".join(dict.fromkeys(out))


def _should_learn_profile(result: ScrapeResult) -> bool:
    return bool(
        result
        and result.real_scrape_evidence
        and result.capture_profile_used
        and result.capture_score >= 70
        and result.capture_decision in {"rich_product_capture", "usable_product_capture", "mixed_capture_needs_review"}
    )

async def run_batch(
    *,
    input_csv: Path,
    output_csv: Path,
    options: BatchOptions,
    summary_json: Path | None = None,
) -> BatchSummary:
    """Run a batch scrape from CSV and write one mapping row per product.

    Results are flushed row-by-row, so interrupted runs still leave a usable
    partial mapping CSV. Use `resume=True` to skip input IDs that already have a
    successful output row.
    """
    started = time.monotonic()
    rows = read_input_csv(input_csv)
    done_ids = _existing_success_ids(output_csv) if options.resume else set()
    append = options.resume and output_csv.exists()
    handle, writer = _prepare_output_csv(output_csv, append=append)
    lock = asyncio.Lock()
    sem = asyncio.Semaphore(max(1, options.max_concurrency))
    base_agent = ProductScrapingAgent(output_root=options.output_root)
    domain_preferences: dict[str, str] = {}

    processed = 0
    skipped = 0
    success = 0
    failed = 0
    manual_review = 0
    quality_counts: dict[str, int] = {}
    input_csv_dir = input_csv.parent

    async def write_row(out_row: dict[str, Any]) -> None:
        async with lock:
            writer.writerow(out_row)
            handle.flush()

    async def process_one(row_number: int, row: dict[str, Any]) -> None:
        nonlocal processed, skipped, success, failed, manual_review
        try:
            input_id_raw = _first(row, *_INPUT_ID_COLUMNS)
            input_id = stable_scrape_id(input_id_raw, row_number)
            if options.resume and input_id in done_ids:
                skipped += 1
                return
            request = request_from_csv_row(
                row,
                row_number=row_number,
                output_root=options.output_root,
                default_retailer_label=options.retailer_label,
                default_max_images=options.max_images,
                default_vision_max=options.vision_max,
                default_max_agent_iterations=options.max_agent_iterations,
                default_write_raw_debug=options.write_raw_debug,
                base_dir=input_csv_dir,
            )
            if options.skip_existing_artifacts:
                expected = options.output_root / (request.scrape_id or input_id) / request.retailer_label / "artifact_manifest.json"
                if expected.exists():
                    skipped += 1
                    return
            domain = _domain_from_url(request.product_url)
            agent = base_agent
            if options.domain_profile_learning and domain and domain in domain_preferences:
                preferred = domain_preferences[domain]
                learned_cfg = replace(
                    base_agent.config,
                    scrape_profile_sequence=_reordered_sequence(base_agent.config.scrape_profile_sequence, preferred),
                )
                agent = ProductScrapingAgent(config=learned_cfg, output_root=options.output_root)
            async with sem:
                result = await agent.scrape(request)
            if options.domain_profile_learning and domain and _should_learn_profile(result):
                async with lock:
                    existing = domain_preferences.get(domain)
                    if not existing or result.capture_score >= 82:
                        domain_preferences[domain] = result.capture_profile_used
            out_row = result_to_output_row(row_number=row_number, input_id=input_id, request=request, result=result)
            processed += 1
            if result.success:
                success += 1
            else:
                failed += 1
            if result.requires_manual_review:
                manual_review += 1
            quality = result.artifact_quality or "not_evaluated"
            quality_counts[quality] = quality_counts.get(quality, 0) + 1
            await write_row(out_row)
        except Exception as exc:
            if options.stop_on_error:
                raise
            try:
                input_id_raw = _first(row, *_INPUT_ID_COLUMNS)
                input_id = stable_scrape_id(input_id_raw, row_number)
                request = request_from_csv_row(
                    row,
                    row_number=row_number,
                    output_root=options.output_root,
                    default_retailer_label=options.retailer_label,
                    default_max_images=options.max_images,
                    default_vision_max=options.vision_max,
                    default_max_agent_iterations=options.max_agent_iterations,
                    default_write_raw_debug=options.write_raw_debug,
                    base_dir=input_csv_dir,
                )
            except Exception:
                input_id = stable_scrape_id("", row_number)
                request = ScrapeRequest(product_url=_first(row, *_URL_COLUMNS, default="about:blank"), scrape_id=input_id, output_root=options.output_root)
            processed += 1
            failed += 1
            manual_review += 1
            quality_counts["not_created"] = quality_counts.get("not_created", 0) + 1
            await write_row(result_to_output_row(row_number=row_number, input_id=input_id, request=request, error=str(exc)))

    tasks = [asyncio.create_task(process_one(i, row)) for i, row in enumerate(rows, start=1)]
    try:
        await asyncio.gather(*tasks)
    finally:
        handle.close()

    summary = BatchSummary(
        input_csv=input_csv,
        output_csv=output_csv,
        output_root=options.output_root,
        total_rows=len(rows),
        processed_rows=processed,
        skipped_rows=skipped,
        success_rows=success,
        failed_rows=failed,
        manual_review_rows=manual_review,
        elapsed_seconds=time.monotonic() - started,
        quality_counts=quality_counts,
        domain_profile_preferences=domain_preferences,
    )
    if summary_json:
        summary_json.parent.mkdir(parents=True, exist_ok=True)
        summary_json.write_text(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


__all__ = [
    "BatchOptions",
    "BatchSummary",
    "DEFAULT_BATCH_OUTPUT_COLUMNS",
    "read_input_csv",
    "request_from_csv_row",
    "result_to_output_row",
    "run_batch",
    "stable_scrape_id",
]
