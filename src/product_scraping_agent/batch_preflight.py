"""Batch input preflight checks.

The scraper writes artifacts by stable input id. Duplicate input ids can make
parallel workers write into the same artifact directory, so the CLI normalizes
duplicates before the batch starts.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .batch import _first, _INPUT_ID_COLUMNS, stable_scrape_id


@dataclass(frozen=True)
class BatchPreflightResult:
    input_csv: Path
    effective_input_csv: Path
    preflight_json: Path | None
    total_rows: int
    duplicate_groups: int
    duplicate_rows: int
    changed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_csv": str(self.input_csv),
            "effective_input_csv": str(self.effective_input_csv),
            "preflight_json": str(self.preflight_json) if self.preflight_json else "",
            "total_rows": self.total_rows,
            "duplicate_groups": self.duplicate_groups,
            "duplicate_rows": self.duplicate_rows,
            "changed": self.changed,
        }


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"Input CSV has no header row: {path}")
        return list(reader.fieldnames), [dict(row) for row in reader]


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def prepare_unique_batch_input_csv(
    input_csv: Path,
    *,
    output_csv: Path,
    preflight_json: Path | None = None,
    fail_on_duplicate_input_id: bool = False,
) -> BatchPreflightResult:
    """Return a collision-safe CSV path for a batch run.

    If duplicate explicit input ids are present, duplicate rows are copied to a
    temporary normalized CSV with suffixed input ids:

    ``ABC`` -> ``ABC`` and ``ABC__DUP02``.

    The original id and duplicate metadata are preserved in extra columns.
    Blank input ids are already row-stabilized by the batch runner and are not
    treated as explicit collisions.
    """
    input_csv = Path(input_csv)
    output_csv = Path(output_csv)
    fieldnames, rows = _read_csv(input_csv)

    explicit_ids: list[str] = []
    for row_number, row in enumerate(rows, start=1):
        raw = _first(row, *_INPUT_ID_COLUMNS)
        explicit_ids.append(stable_scrape_id(raw, row_number) if raw else "")

    counts: dict[str, int] = {}
    for sid in explicit_ids:
        if sid:
            counts[sid] = counts.get(sid, 0) + 1
    duplicate_ids = {sid for sid, count in counts.items() if count > 1}
    duplicate_rows = sum(1 for sid in explicit_ids if sid in duplicate_ids)

    report_rows: list[dict[str, Any]] = []
    if duplicate_ids and fail_on_duplicate_input_id:
        raise ValueError(
            "Duplicate input_id values would collide in artifact output folders: "
            + ", ".join(sorted(duplicate_ids)[:20])
        )

    if not duplicate_ids:
        if preflight_json:
            preflight_json.parent.mkdir(parents=True, exist_ok=True)
            preflight_json.write_text(
                json.dumps(
                    {
                        "input_csv": str(input_csv),
                        "effective_input_csv": str(input_csv),
                        "total_rows": len(rows),
                        "duplicate_groups": 0,
                        "duplicate_rows": 0,
                        "changed": False,
                        "rows": [],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        return BatchPreflightResult(input_csv, input_csv, preflight_json, len(rows), 0, 0, False)

    fieldnames_out = list(fieldnames)
    for col in (
        "original_input_id",
        "input_id_duplicate_count",
        "input_id_duplicate_index",
        "input_id_collision_resolved",
    ):
        if col not in fieldnames_out:
            fieldnames_out.append(col)
    if "input_id" not in fieldnames_out:
        fieldnames_out.insert(0, "input_id")

    seen: dict[str, int] = {}
    normalized: list[dict[str, Any]] = []
    for row_number, row in enumerate(rows, start=1):
        out = dict(row)
        original_raw = _first(row, *_INPUT_ID_COLUMNS)
        stable = stable_scrape_id(original_raw, row_number) if original_raw else stable_scrape_id("", row_number)
        if stable in duplicate_ids:
            seen[stable] = seen.get(stable, 0) + 1
            dup_index = seen[stable]
            resolved = stable if dup_index == 1 else f"{stable}__DUP{dup_index:02d}"
            out["input_id"] = resolved
            out["original_input_id"] = original_raw or stable
            out["input_id_duplicate_count"] = counts[stable]
            out["input_id_duplicate_index"] = dup_index
            out["input_id_collision_resolved"] = resolved != stable
            report_rows.append(
                {
                    "row_number": row_number,
                    "original_input_id": original_raw or stable,
                    "resolved_input_id": resolved,
                    "duplicate_count": counts[stable],
                    "duplicate_index": dup_index,
                    "changed": resolved != stable,
                }
            )
        else:
            out.setdefault("input_id", original_raw or stable)
            out.setdefault("original_input_id", original_raw or stable)
            out.setdefault("input_id_duplicate_count", 1)
            out.setdefault("input_id_duplicate_index", 1)
            out.setdefault("input_id_collision_resolved", False)
        normalized.append(out)

    effective_csv = output_csv.with_suffix(output_csv.suffix + ".preflight_input.csv")
    _write_csv(effective_csv, fieldnames_out, normalized)

    if preflight_json is None:
        preflight_json = output_csv.with_suffix(output_csv.suffix + ".preflight.json")
    preflight_json.parent.mkdir(parents=True, exist_ok=True)
    preflight_json.write_text(
        json.dumps(
            {
                "input_csv": str(input_csv),
                "effective_input_csv": str(effective_csv),
                "total_rows": len(rows),
                "duplicate_groups": len(duplicate_ids),
                "duplicate_rows": duplicate_rows,
                "changed": True,
                "duplicate_ids": sorted(duplicate_ids),
                "rows": report_rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return BatchPreflightResult(input_csv, effective_csv, preflight_json, len(rows), len(duplicate_ids), duplicate_rows, True)


__all__ = ["BatchPreflightResult", "prepare_unique_batch_input_csv"]
