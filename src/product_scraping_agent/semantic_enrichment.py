"""Contract-safe semantic enrichment for scraped product artifacts.

The enrichment is intentionally schema-stable:
- no new required artifact files
- no artifact file renames
- no top-level product_evidence.json schema replacement
- no search or new scraping provider

It rewrites existing artifact files in-place to make them easier for the
product-coding engine to consume: clearer identity status, feature evidence
readiness, provenance hints, and trust/review/ignore guidance.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_FEATURE_ALIASES: dict[str, list[str]] = {
    "brand": ["brand", "marca"],
    "manufacturer": ["manufacturer", "toy manuf", "producer", "výrobce", "fabricante"],
    "ean_gtin": ["ean", "gtin", "barcode", "čárový", "codigo de barras"],
    "sku_mpn": ["sku", "mpn", "model", "item number", "article"],
    "product_type": ["type", "category", "product type", "toy type"],
    "age_range": ["age", "recommended age", "edad", "věk", "3+", "years"],
    "piece_count": ["piece", "pieces", "pcs", "ks", "parts", "number of pieces", "count"],
    "package_contents": ["contents", "included", "package contents", "contains", "accessories"],
    "material": ["material", "plastic", "wood", "metal", "plush", "textile"],
    "dimensions": ["dimension", "size", "height", "width", "length", "cm", "mm", "weight"],
    "battery_required": ["battery", "batteries", "electronic", "light", "sound"],
    "safety_warnings": ["warning", "safety", "choking", "not suitable", "small parts"],
    "license_or_character": ["character", "franchise", "license", "disney", "marvel", "pokemon", "paw patrol"],
    "assortment_or_set_count": ["assortment", "set", "bundle", "pack", "tube", "multipack"],
}

_PRODUCT_LEVEL_LISTS = [
    "retailer_claims",
    "structured_claims",
    "table_claims",
    "visual_claims",
    "upstream_indexed_claims",
    "url_derived_claims",
    "input_context_claims",
]
_SOURCE_SPECIFIC_LISTS = ["source_specific_claims"]


@dataclass
class ArtifactEnrichmentResult:
    scrape_id: str
    artifact_dir: Path
    changed_files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scrape_id": self.scrape_id,
            "artifact_dir": str(self.artifact_dir),
            "changed_files": self.changed_files,
            "warnings": self.warnings,
        }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if path.exists() and path.is_file():
            obj = json.loads(path.read_text(encoding="utf-8"))
            return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}
    return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


def _read_text(path: Path) -> str:
    try:
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return ""


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _tokens(text: str) -> set[str]:
    return {
        t for t in re.split(r"[^a-zA-Z0-9]+", (text or "").lower())
        if len(t) >= 3 and t not in {"the", "and", "for", "with", "product", "item", "toy", "toys"}
    }


def _match_score(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return round(len(ta & tb) / max(1, len(ta)), 3)


def _digits(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _cell(value: Any, max_len: int = 260) -> str:
    text = re.sub(r"\s+", " ", _as_text(value)).strip().replace("|", "\\|")
    if len(text) > max_len:
        return text[: max_len - 1].rstrip() + "…"
    return text


def _claim_value(claim: dict[str, Any]) -> str:
    for key in ("value", "normalized_value", "claim", "content", "description", "text"):
        val = claim.get(key)
        if val not in (None, "", [], {}):
            return _as_text(val)
    return ""


def _claim_attribute(claim: dict[str, Any]) -> str:
    for key in ("attribute", "field", "heading", "type", "claim_type", "claim_id"):
        val = claim.get(key)
        if val not in (None, ""):
            return str(val)
    return "claim"


def _claim_blob(claim: dict[str, Any]) -> str:
    return " ".join([_claim_attribute(claim), _claim_value(claim), _as_text(claim.get("raw_text")), _as_text(claim.get("source_refs"))]).lower()


def _iter_claims(evidence: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for list_name in [*_PRODUCT_LEVEL_LISTS, *_SOURCE_SPECIFIC_LISTS]:
        rows = evidence.get(list_name) or []
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    out.append((list_name, row))
    identity = evidence.get("product_identity") or {}
    if isinstance(identity, dict):
        for key, val in identity.items():
            if isinstance(val, dict):
                pseudo = dict(val)
                pseudo.setdefault("attribute", key)
                pseudo.setdefault("claim_scope", "product_identity")
                out.append(("product_identity", pseudo))
    return out


def _infer_feature(attribute: str, value: str) -> str:
    blob = f"{attribute} {value}".lower()
    for feature, aliases in _FEATURE_ALIASES.items():
        if any(alias in blob for alias in aliases):
            return feature
    return "general_product_fact"


def _enrich_claim_rows(evidence: dict[str, Any]) -> int:
    changed = 0
    for list_name in [*_PRODUCT_LEVEL_LISTS, *_SOURCE_SPECIFIC_LISTS]:
        rows = evidence.get(list_name)
        if not isinstance(rows, list):
            continue
        for claim in rows:
            if not isinstance(claim, dict):
                continue
            attr = _claim_attribute(claim)
            value = _claim_value(claim)
            feature = _infer_feature(attr, value)
            defaults = {
                "normalized_value": value,
                "coding_relevance": feature,
                "evidence_summary": _cell(value or claim, 420),
                "downstream_action": "source_specific_only" if list_name in _SOURCE_SPECIFIC_LISTS else "use_if_identity_and_quality_pass",
                "transferability": "scraped_source_only" if list_name in _SOURCE_SPECIFIC_LISTS else "product_level_transferable_when_identity_passes",
                "conflict_status": "not_evaluated" if not claim.get("confidence") else "no_conflict_reported",
                "missing_reason": "" if value else "value missing or not directly extracted",
                "decision_note": "Use only with listed evidence axes/source refs; do not infer unsupported feature values.",
            }
            for k, v in defaults.items():
                if k not in claim:
                    claim[k] = v
                    changed += 1
            if "source_refs" not in claim:
                claim["source_refs"] = []
                changed += 1
            if "confidence" not in claim:
                claim["confidence"] = "medium" if value else "missing"
                changed += 1
    return changed


def _find_feature_evidence(evidence: dict[str, Any], feature: str, aliases: list[str]) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for list_name, claim in _iter_claims(evidence):
        blob = _claim_blob(claim)
        if any(alias.lower() in blob for alias in aliases):
            matches.append({
                "claim_source": list_name,
                "attribute": _claim_attribute(claim),
                "value": _claim_value(claim),
                "evidence_axis": claim.get("evidence_axis") or claim.get("evidence_axes") or [],
                "source_refs": claim.get("source_refs") or claim.get("sources") or [],
                "confidence": claim.get("confidence") or "medium",
            })
    if not matches:
        return {
            "feature_hint": feature,
            "status": "missing",
            "evidence_fields": [],
            "source_files": [],
            "coding_instruction": f"Do not infer {feature}; no direct evidence found in artifact.",
        }
    strong = any(str(m.get("confidence", "")).lower() == "high" for m in matches)
    axes = sorted({str(a) for m in matches for a in (m.get("evidence_axis") or []) if a})
    return {
        "feature_hint": feature,
        "status": "strong" if strong or len(matches) >= 2 or len(axes) >= 2 else "medium",
        "evidence_fields": [f"{m['claim_source']}.{m['attribute']}" for m in matches[:6]],
        "source_files": sorted({str(r) for m in matches for r in (m.get("source_refs") or []) if r})[:8],
        "supporting_values": [m.get("value") for m in matches[:6] if m.get("value")],
        "evidence_axes": axes,
        "coding_instruction": f"Use {feature} only from listed evidence; leave unsupported subfeatures missing.",
    }


def _identity_value(evidence: dict[str, Any], key: str) -> str:
    ident = evidence.get("product_identity") or {}
    val = ident.get(key) if isinstance(ident, dict) else None
    if isinstance(val, dict):
        return str(val.get("value") or "")
    return str(val or "")


def _build_identity_verification(evidence: dict[str, Any], metadata: dict[str, Any], quality: dict[str, Any]) -> dict[str, Any]:
    input_context = metadata.get("input_context") or evidence.get("input_context") or {}
    requested_main = str(input_context.get("main_text") or "")
    requested_ean = str(input_context.get("ean") or "")
    requested_retailer = str(input_context.get("retailer_name") or "")
    requested_country = str(input_context.get("country_code") or "")
    product_title = _identity_value(evidence, "product_name") or str(metadata.get("title") or "")
    brand = _identity_value(evidence, "brand")
    manufacturer = _identity_value(evidence, "manufacturer")
    ean_detected = _identity_value(evidence, "ean_gtin")
    score = _match_score(requested_main, product_title)
    ean_match = bool(requested_ean and ean_detected and _digits(requested_ean) == _digits(ean_detected))
    if ean_match:
        status = "strong"
    elif requested_main and score >= 0.65:
        status = "strong"
    elif requested_main and score >= 0.40:
        status = "medium"
    elif requested_main and product_title:
        status = "weak"
    elif product_title or brand or manufacturer:
        status = "unknown"
    else:
        status = "unknown"
    if requested_ean and ean_detected and not ean_match:
        status = "wrong_item" if score < 0.35 else "weak"
    reasons: list[str] = []
    if requested_main:
        reasons.append(f"main_text/title token match score={score}")
    if requested_ean:
        reasons.append("EAN matched" if ean_match else "EAN not matched or not found")
    if product_title:
        reasons.append("product title available in evidence")
    if not product_title:
        reasons.append("product title missing from evidence")
    return {
        "requested_main_text": requested_main,
        "requested_ean": requested_ean,
        "requested_retailer": requested_retailer,
        "requested_country": requested_country,
        "product_title": product_title,
        "brand_detected": brand,
        "manufacturer_detected": manufacturer,
        "ean_detected": ean_detected,
        "main_text_match_score": score,
        "ean_match_status": "matched" if ean_match else ("not_provided" if not requested_ean else "not_matched_or_missing"),
        "identity_status": status,
        "identity_reasons": reasons,
        "page_type": quality.get("page_classification_status") or quality.get("capture_decision") or "not_evaluated",
    }


def _build_coding_readiness(quality: dict[str, Any], identity: dict[str, Any], readiness: list[dict[str, Any]]) -> dict[str, Any]:
    artifact_quality = str(quality.get("artifact_quality") or quality.get("quality_gate") or "not_evaluated")
    visual_status = str((quality.get("visual_evidence") or {}).get("visual_evidence_status") or quality.get("visual_evidence_status") or "")
    missing = quality.get("missing_critical_fields") or []
    missing_features = [r["feature_hint"] for r in readiness if r.get("status") == "missing"]
    ready = bool(
        identity.get("identity_status") in {"strong", "medium"}
        and artifact_quality in {"strong", "usable"}
        and visual_status in {"", "final_product_images_available"}
        and not quality.get("requires_manual_review", False)
    )
    if identity.get("identity_status") == "wrong_item":
        action = "do_not_code_rescrape"
    elif ready:
        action = "safe_for_automated_product_coding"
    elif artifact_quality in {"partial", "usable"}:
        action = "code_supported_features_only_and_review_gaps"
    else:
        action = "manual_review_or_rescrape_recommended"
    return {
        "ready_for_coding": ready,
        "recommended_downstream_action": action,
        "identity_status": identity.get("identity_status"),
        "artifact_quality": artifact_quality,
        "visual_evidence_status": visual_status,
        "critical_gaps": missing,
        "unsupported_feature_hints": missing_features,
        "safe_product_level_fact_policy": "Use only facts with evidence axes/source refs and identity_status strong/medium.",
        "source_specific_fact_policy": "Price, availability, seller, delivery, shipping, rating, and marketplace terms remain scraped-source scoped.",
    }


def _markdown_readiness_section(quality: dict[str, Any]) -> str:
    sem = quality.get("semantic_enrichment") or {}
    identity = sem.get("identity_verification") or {}
    coding = sem.get("coding_readiness") or {}
    readiness = sem.get("feature_evidence_readiness") or []
    lines = [
        "## Downstream Product-Coding Readiness",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Ready for coding | {_cell(coding.get('ready_for_coding'))} |",
        f"| Recommended action | {_cell(coding.get('recommended_downstream_action'))} |",
        f"| Identity status | {_cell(identity.get('identity_status'))} |",
        f"| Main-text match score | {_cell(identity.get('main_text_match_score'))} |",
        f"| EAN match status | {_cell(identity.get('ean_match_status'))} |",
        f"| Artifact quality | {_cell(coding.get('artifact_quality'))} |",
        f"| Visual evidence status | {_cell(coding.get('visual_evidence_status'))} |",
        "",
        "### Feature Evidence Readiness",
        "",
        "| Feature hint | Status | Supporting values | Coding instruction |",
        "|---|---|---|---|",
    ]
    for row in readiness:
        lines.append(
            f"| {_cell(row.get('feature_hint'))} | {_cell(row.get('status'))} | "
            f"{_cell(row.get('supporting_values') or [])} | {_cell(row.get('coding_instruction'), 420)} |"
        )
    return "\n".join(lines).strip() + "\n"


def _prepend_once(text: str, marker: str, section: str) -> str:
    if marker in text:
        return text
    return section.rstrip() + "\n\n" + (text or "").lstrip()


def enrich_artifact_dir(row_dir: Path, *, retailer_label: str = "retailer") -> ArtifactEnrichmentResult:
    row_dir = Path(row_dir)
    scrape_id = row_dir.name
    retailer_dir = row_dir / retailer_label
    result = ArtifactEnrichmentResult(scrape_id=scrape_id, artifact_dir=row_dir)
    product_json = retailer_dir / "product_evidence.json"
    quality_json = retailer_dir / "quality_report.json"
    metadata_json = retailer_dir / "metadata.json"
    source_md = retailer_dir / "source.md"
    evidence_md = retailer_dir / "product_evidence.md"
    claims_md = retailer_dir / "claims.md"

    evidence = _read_json(product_json)
    if not evidence:
        result.warnings.append("product_evidence.json missing or invalid; skipped semantic enrichment")
        return result
    metadata = _read_json(metadata_json)
    quality = _read_json(quality_json)
    claim_changes = _enrich_claim_rows(evidence)
    quality_section = evidence.setdefault("quality", {})
    if not isinstance(quality_section, dict):
        quality_section = {}
        evidence["quality"] = quality_section
    merged_quality = {**quality_section, **quality}
    identity = _build_identity_verification(evidence, metadata, merged_quality)
    readiness = [_find_feature_evidence(evidence, feature, aliases) for feature, aliases in _FEATURE_ALIASES.items()]
    coding = _build_coding_readiness(merged_quality, identity, readiness)
    sem = {
        "contract_safe": True,
        "schema_policy": "No artifact files or top-level artifact contract changed; existing files were enriched in-place.",
        "identity_verification": identity,
        "feature_evidence_readiness": readiness,
        "coding_readiness": coding,
        "claim_row_enrichment_count": claim_changes,
    }
    quality_section["semantic_enrichment"] = sem
    quality["semantic_enrichment"] = sem
    if "recommended_followups" in quality and isinstance(quality["recommended_followups"], list):
        if coding["recommended_downstream_action"] != "safe_for_automated_product_coding":
            quality["recommended_followups"].append(coding["recommended_downstream_action"])
    _write_json(product_json, evidence)
    result.changed_files.append(str(product_json.relative_to(row_dir)))
    _write_json(quality_json, quality)
    result.changed_files.append(str(quality_json.relative_to(row_dir)))

    section = _markdown_readiness_section(quality)
    for path in (source_md, evidence_md, claims_md):
        text = _read_text(path)
        if text:
            _write_text(path, _prepend_once(text, "## Downstream Product-Coding Readiness", section))
            result.changed_files.append(str(path.relative_to(row_dir)))
    return result


def enrich_artifact_root(output_root: Path, *, retailer_label: str = "retailer") -> list[ArtifactEnrichmentResult]:
    output_root = Path(output_root)
    if not output_root.exists():
        return []
    results: list[ArtifactEnrichmentResult] = []
    for child in sorted(output_root.iterdir()):
        if child.is_dir():
            results.append(enrich_artifact_dir(child, retailer_label=retailer_label))
    return results


__all__ = ["ArtifactEnrichmentResult", "enrich_artifact_dir", "enrich_artifact_root"]
