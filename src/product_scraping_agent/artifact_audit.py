"""Post-run artifact audit for product scraping batches.

This module scans a scraped artifact root and reports rows that are incomplete,
visually unusable, or missing required files. It does not scrape or modify any
product evidence.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REQUIRED_ROOT_FILES = ("request.json", "scrape_result.json")
REQUIRED_RETAILER_FILES = (
    "source.md",
    "product_evidence.json",
    "quality_report.json",
    "source_alignment_report.json",
    "vision.md",
)
REQUIRED_MANIFEST_FILES = ("artifact_manifest.json", "image_manifest.json", "agent_trace.json")
READY_VISUAL_STATUS = "final_product_images_available"

AUDIT_COLUMNS = [
    "scrape_id",
    "artifact_dir",
    "artifact_status",
    "ready_for_coding",
    "has_terminal_marker",
    "terminal_marker",
    "missing_files",
    "empty_files",
    "vision_md_empty",
    "images_dir_exists",
    "image_file_count",
    "visual_evidence_status",
    "artifact_quality",
    "requires_manual_review",
    "manual_review_bucket",
    "error",
]


@dataclass
class ArtifactAuditRow:
    scrape_id: str
    artifact_dir: Path
    artifact_status: str
    ready_for_coding: bool
    has_terminal_marker: bool
    terminal_marker: str = ""
    missing_files: list[str] = field(default_factory=list)
    empty_files: list[str] = field(default_factory=list)
    vision_md_empty: bool = False
    images_dir_exists: bool = False
    image_file_count: int = 0
    visual_evidence_status: str = ""
    artifact_quality: str = ""
    requires_manual_review: bool = True
    manual_review_bucket: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "scrape_id": self.scrape_id,
            "artifact_dir": str(self.artifact_dir),
            "artifact_status": self.artifact_status,
            "ready_for_coding": self.ready_for_coding,
            "has_terminal_marker": self.has_terminal_marker,
            "terminal_marker": self.terminal_marker,
            "missing_files": "; ".join(self.missing_files),
            "empty_files": "; ".join(self.empty_files),
            "vision_md_empty": self.vision_md_empty,
            "images_dir_exists": self.images_dir_exists,
            "image_file_count": self.image_file_count,
            "visual_evidence_status": self.visual_evidence_status,
            "artifact_quality": self.artifact_quality,
            "requires_manual_review": self.requires_manual_review,
            "manual_review_bucket": self.manual_review_bucket,
            "error": self.error,
        }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if path.exists() and path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    return {}


def _is_empty_file(path: Path) -> bool:
    try:
        return path.exists() and path.is_file() and path.stat().st_size == 0
    except OSError:
        return False


def _image_count(images_dir: Path) -> int:
    if not images_dir.exists() or not images_dir.is_dir():
        return 0
    exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
    return sum(1 for p in images_dir.rglob("*") if p.is_file() and p.suffix.lower() in exts)


def audit_artifact_dir(row_dir: Path, *, retailer_label: str = "retailer") -> ArtifactAuditRow:
    scrape_id = row_dir.name
    retailer_dir = row_dir / retailer_label
    manifests_dir = retailer_dir / "manifests"

    missing: list[str] = []
    empty: list[str] = []

    for name in REQUIRED_ROOT_FILES:
        path = row_dir / name
        if not path.exists():
            missing.append(name)
        elif _is_empty_file(path):
            empty.append(name)

    for name in REQUIRED_RETAILER_FILES:
        path = retailer_dir / name
        rel = f"{retailer_label}/{name}"
        if not path.exists():
            missing.append(rel)
        elif _is_empty_file(path):
            empty.append(rel)

    for name in REQUIRED_MANIFEST_FILES:
        path = manifests_dir / name
        rel = f"{retailer_label}/manifests/{name}"
        if not path.exists():
            missing.append(rel)
        elif _is_empty_file(path):
            empty.append(rel)

    complete_marker = row_dir / "_COMPLETE.json"
    failed_marker = row_dir / "_FAILED.json"
    if complete_marker.exists():
        terminal_marker = "_COMPLETE.json"
    elif failed_marker.exists():
        terminal_marker = "_FAILED.json"
    else:
        terminal_marker = ""

    scrape_result = _read_json(row_dir / "scrape_result.json")
    quality_report = _read_json(retailer_dir / "quality_report.json")
    artifact_manifest = _read_json(manifests_dir / "artifact_manifest.json")

    visual_status = str(
        scrape_result.get("visual_evidence_status")
        or artifact_manifest.get("quality", {}).get("visual_evidence_status")
        or artifact_manifest.get("counts", {}).get("visual_evidence_status")
        or ""
    )
    artifact_quality = str(
        scrape_result.get("artifact_quality")
        or quality_report.get("artifact_quality")
        or artifact_manifest.get("quality", {}).get("quality_gate")
        or ""
    )
    requires_review = bool(
        scrape_result.get("requires_manual_review", True)
        if "requires_manual_review" in scrape_result
        else quality_report.get("requires_manual_review", True)
    )
    manual_bucket = str(scrape_result.get("manual_review_bucket") or "")
    error = str(scrape_result.get("error") or _read_json(failed_marker).get("error") or "")

    vision_path = retailer_dir / "vision.md"
    vision_empty = (not vision_path.exists()) or _is_empty_file(vision_path) or not vision_path.read_text(encoding="utf-8", errors="replace").strip()
    images_dir = retailer_dir / "images"
    img_count = _image_count(images_dir)

    has_terminal = bool(terminal_marker)
    ready = bool(
        has_terminal
        and terminal_marker == "_COMPLETE.json"
        and not missing
        and not empty
        and not vision_empty
        and visual_status == READY_VISUAL_STATUS
        and img_count >= 1
        and not requires_review
        and artifact_quality in {"strong", "usable"}
    )

    if not has_terminal:
        status = "incomplete_no_terminal_marker"
    elif terminal_marker == "_FAILED.json":
        status = "failed_finalized"
    elif missing or empty or vision_empty:
        status = "complete_but_artifact_files_invalid"
    elif visual_status != READY_VISUAL_STATUS or img_count < 1:
        status = "complete_but_visual_not_ready"
    elif requires_review:
        status = "complete_but_manual_review_required"
    elif ready:
        status = "ready_for_coding"
    else:
        status = "complete_but_needs_review"

    return ArtifactAuditRow(
        scrape_id=scrape_id,
        artifact_dir=row_dir,
        artifact_status=status,
        ready_for_coding=ready,
        has_terminal_marker=has_terminal,
        terminal_marker=terminal_marker,
        missing_files=missing,
        empty_files=empty,
        vision_md_empty=vision_empty,
        images_dir_exists=images_dir.exists() and images_dir.is_dir(),
        image_file_count=img_count,
        visual_evidence_status=visual_status,
        artifact_quality=artifact_quality,
        requires_manual_review=requires_review,
        manual_review_bucket=manual_bucket,
        error=error,
    )


def audit_artifact_root(output_root: Path, *, retailer_label: str = "retailer") -> list[ArtifactAuditRow]:
    output_root = Path(output_root)
    if not output_root.exists():
        return []
    rows: list[ArtifactAuditRow] = []
    for child in sorted(output_root.iterdir()):
        if child.is_dir():
            rows.append(audit_artifact_dir(child, retailer_label=retailer_label))
    return rows


def write_audit_outputs(rows: list[ArtifactAuditRow], *, output_csv: Path, output_json: Path | None = None) -> dict[str, Any]:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=AUDIT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.as_dict())

    summary: dict[str, Any] = {
        "total_artifacts": len(rows),
        "ready_for_coding": sum(1 for r in rows if r.ready_for_coding),
        "not_ready": sum(1 for r in rows if not r.ready_for_coding),
        "missing_terminal_marker": sum(1 for r in rows if not r.has_terminal_marker),
        "failed_finalized": sum(1 for r in rows if r.terminal_marker == "_FAILED.json"),
        "vision_md_empty": sum(1 for r in rows if r.vision_md_empty),
        "missing_or_empty_files": sum(1 for r in rows if r.missing_files or r.empty_files),
        "missing_clean_image": sum(1 for r in rows if r.visual_evidence_status != READY_VISUAL_STATUS or r.image_file_count < 1),
        "status_counts": {},
    }
    for row in rows:
        summary["status_counts"][row.artifact_status] = summary["status_counts"].get(row.artifact_status, 0) + 1

    if output_json:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


__all__ = ["AUDIT_COLUMNS", "ArtifactAuditRow", "audit_artifact_dir", "audit_artifact_root", "write_audit_outputs"]
