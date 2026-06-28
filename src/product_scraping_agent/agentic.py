"""LLM-driven same-page planning and product-only evidence normalization."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from .full_scraper import FullPage
from .log import logger
from .models import AgentPlan, ImageRef, ProductInputContext, ProductEvidence, TableRef
from .prompts import P
from .text_utils import truncate_text

_MD_PLAN_CHARS = 18_000
_MD_EVIDENCE_CHARS = 75_000
_HTML_SIGNAL_CHARS = 12_000
_TABLE_MAX_CHARS = 7_500
_VISION_MAX_CHARS = 16_000


def _strip_json_fence(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I).strip()
        text = re.sub(r"\s*```$", "", text).strip()
    # Best-effort if gateway prepends prose despite JSON mode absence.
    if not (text.startswith("{") or text.startswith("[")):
        m = re.search(r"(\{.*\})", text, flags=re.S)
        if m:
            text = m.group(1)
    return text


def _json_loads_object(text: str) -> dict[str, Any]:
    raw = _strip_json_fence(text)
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError("expected JSON object")
    return obj


def page_observation_summary(page: FullPage, input_context: ProductInputContext, product_hint: str) -> str:
    """Compact planner context — enough for gap detection, not full final evidence."""
    head = {
        "requested_url": page.url,
        "final_url": page.final_url or page.url,
        "title": page.title,
        "description": page.description,
        "canonical_url": page.canonical_url,
        "profiles_merged": page.profiles_merged,
        "counts": {
            "markdown_chars": len(page.raw_markdown or ""),
            "html_chars": len(page.raw_html or ""),
            "image_candidates": len(page.images),
            "tables_html": len(page.tables_html),
            "json_ld_blocks": len(page.json_ld),
        },
        "input_context": input_context.model_dump(),
        "product_hint": product_hint,
        "structured_keys": {
            "og": sorted(page.og.keys())[:50],
            "product_meta": sorted(page.product_meta.keys())[:50],
        },
    }
    markdown_sample = truncate_text(page.raw_markdown or "", _MD_PLAN_CHARS)
    jsonld_sample = truncate_text(json.dumps(page.json_ld, ensure_ascii=False, indent=2), 8000)
    return (
        "# Page capture summary\n"
        f"```json\n{json.dumps(head, ensure_ascii=False, indent=2)}\n```\n\n"
        "# JSON-LD sample\n"
        f"```json\n{jsonld_sample}\n```\n\n"
        "# Rendered markdown sample\n"
        f"{markdown_sample or '(empty)'}\n"
    )


def plan_next_actions(page: FullPage, input_context: ProductInputContext, product_hint: str) -> AgentPlan:
    """Ask the LLM whether more same-page scraping is required."""
    from .services.llm import get_llm_service

    schema = {
        "enough_evidence": False,
        "missing_evidence": ["which product-specific evidence is missing, if any"],
        "actions": [
            {
                "action": "full_page_scroll | expand_common_sections | extract_gallery_sources | retry_relaxed | stop",
                "reason": "why this same-page action is needed",
                "priority": 1,
            }
        ],
        "stop_reason": "why no more same-page scraping is needed",
    }
    user = (
        "Decide if another same-product-URL scrape pass is required.\n"
        "Allowed actions are exactly: stop, full_page_scroll, expand_common_sections, "
        "extract_gallery_sources, retry_relaxed. Do not request web search.\n\n"
        "Return JSON with this shape:\n"
        f"```json\n{json.dumps(schema, ensure_ascii=False, indent=2)}\n```\n\n"
        f"{page_observation_summary(page, input_context, product_hint)}"
    )
    resp = get_llm_service().predict(
        user,
        system_prompt=P.SCRAPE_PLANNER.system,
        max_tokens=1600,
        temperature=0.0,
        response_format={"type": "json_object"},
        purpose=P.SCRAPE_PLANNER.name,
    )
    obj = _json_loads_object(resp.content)
    try:
        return AgentPlan.model_validate(obj)
    except Exception as exc:
        logger.warning("planner JSON did not validate; using safe stop. error={}", exc)
        return AgentPlan(enough_evidence=True, actions=[], stop_reason="planner output invalid; stop safely")


def _structured_axis(page: FullPage) -> dict[str, Any]:
    return {
        "requested_url": page.url,
        "final_url": page.final_url or page.url,
        "title": page.title,
        "description": page.description,
        "canonical_url": page.canonical_url,
        "og": page.og,
        "product_meta": page.product_meta,
        "json_ld": page.json_ld,
        "profiles_merged": page.profiles_merged,
    }


def _tables_axis(tables: list[TableRef]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in tables:
        out.append({
            "index": t.index,
            "caption": t.caption,
            "rows": t.rows,
            "cols": t.cols,
            "markdown": truncate_text(t.markdown or "", _TABLE_MAX_CHARS),
        })
    return out


def _visual_axis(images: list[ImageRef]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, img in enumerate(images, start=1):
        if not img.description:
            continue
        out.append({
            "index": i,
            "local_path": img.local_path.name if img.local_path else "",
            "alt": img.alt,
            "relevance": img.relevance,
            "description": truncate_text(img.description, 1200),
        })
    return out


def normalize_product_evidence(
    *,
    page: FullPage,
    tables: list[TableRef],
    images: list[ImageRef],
    input_context: ProductInputContext,
    product_hint: str,
    scrape_id: str,
) -> ProductEvidence:
    """Produce the main noise-free product evidence JSON using the LLM."""
    from .services.llm import get_llm_service

    expected_schema = {
        "product_focus_summary": "1-3 sentence retailer-claim summary, no guesses",
        "product_identity": {
            "product_name": {"value": "", "evidence_axis": ["T"], "source_refs": [], "confidence": "high|medium|low|missing"},
            "brand": {"value": "", "evidence_axis": ["T", "V", "S", "D"], "source_refs": [], "confidence": "high|medium|low|missing"},
            "ean_gtin": {"value": "", "evidence_axis": ["S", "D", "T"], "source_refs": [], "confidence": "high|medium|low|missing"},
            "sku_mpn": {"value": "", "evidence_axis": [], "source_refs": [], "confidence": "high|medium|low|missing"},
            "manufacturer": {"value": "", "evidence_axis": [], "source_refs": [], "confidence": "high|medium|low|missing"},
            "retailer": {"value": "", "evidence_axis": ["I", "S"], "source_refs": [], "confidence": "high|medium|low|missing"},
        },
        "retailer_claims": [
            {
                "claim_id": "C001",
                "attribute": "age_range | piece_count | material | contents | features | category | dimensions | battery | warning | price | availability | etc",
                "value": "exact value as supported by retailer evidence",
                "claim": "complete grounded claim sentence",
                "evidence_axis": ["T"],
                "source_refs": ["T: short quote or section label"],
                "confidence": "high|medium|low",
                "notes": "",
            }
        ],
        "product_only_text_blocks": [
            {"heading": "Product description", "content": "clean product-only text, no nav/footer/recommendations", "evidence_axis": ["T"]}
        ],
        "structured_claims": [],
        "table_claims": [],
        "visual_claims": [],
        "discrepancies": [],
        "gaps": [],
        "noise_exclusion_summary": {
            "policy": "product-only; excluded unrelated page/site content",
            "excluded_categories": ["navigation", "footer", "recommendations", "cookie text", "generic shipping/payment boilerplate", "ads", "unrelated products"],
            "notes": []
        },
        "quality": {
            "product_page_confidence": "high|medium|low",
            "evidence_completeness": "high|medium|low",
            "has_text_evidence": True,
            "has_structured_evidence": True,
            "has_table_evidence": True,
            "has_visual_evidence": True,
            "agentic_iterations_used": 0,
        },
    }

    payload = {
        "scrape_id": scrape_id,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "input_context": input_context.model_dump(),
        "product_hint": product_hint,
        "axis_S_structured": _structured_axis(page),
        "axis_D_tables": _tables_axis(tables),
        "axis_V_visual": _visual_axis(images),
        "axis_T_rendered_markdown": truncate_text(page.raw_markdown or "", _MD_EVIDENCE_CHARS),
        # Small HTML signal helps when text markdown misses alt/data attributes; still not a raw dump.
        "html_signal_sample": truncate_text(page.raw_html or "", _HTML_SIGNAL_CHARS),
    }
    user = (
        "Build the complete product-only retailer evidence JSON. Remove noise; do not summarize noisy content. "
        "Preserve all product facts that the retailer page claims through text, tables, structured metadata, or images. "
        "Do not guess and do not use external knowledge.\n\n"
        "Return a JSON object matching this schema pattern:\n"
        f"```json\n{json.dumps(expected_schema, ensure_ascii=False, indent=2)}\n```\n\n"
        "Captured evidence payload:\n"
        f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```"
    )
    resp = get_llm_service().predict(
        user,
        system_prompt=P.PRODUCT_EVIDENCE_JSON.system,
        max_tokens=8192,
        temperature=0.0,
        response_format={"type": "json_object"},
        purpose=P.PRODUCT_EVIDENCE_JSON.name,
    )
    obj = _json_loads_object(resp.content)
    obj.setdefault("quality", {})["created_by"] = "llm_product_evidence_normalizer"
    return ProductEvidence.model_validate(obj)


def deterministic_product_evidence(
    *,
    page: FullPage,
    tables: list[TableRef],
    images: list[ImageRef],
    input_context: ProductInputContext,
    product_hint: str,
    reason: str,
) -> ProductEvidence:
    """Safe fallback when the LLM is unavailable; does not pretend to be complete."""
    return ProductEvidence(
        product_focus_summary=(
            "LLM product-only normalization was unavailable. This fallback preserves only high-level captured "
            "signals and should not be treated as a complete normalized artifact."
        ),
        product_identity={
            "page_title": {"value": page.title, "evidence_axis": ["S", "T"], "source_refs": ["metadata:title"], "confidence": "medium" if page.title else "missing"},
            "canonical_url": {"value": page.canonical_url, "evidence_axis": ["S"], "source_refs": ["metadata:canonical"], "confidence": "medium" if page.canonical_url else "missing"},
            "input_context": input_context.model_dump(),
        },
        retailer_claims=[],
        product_only_text_blocks=[
            {
                "heading": "Captured rendered text sample — unnormalized fallback",
                "content": truncate_text(page.raw_markdown or "", 12_000),
                "evidence_axis": ["T"],
            }
        ],
        structured_claims=[{"source": "metadata/json_ld", "value": _structured_axis(page)}],
        table_claims=[{"table_index": t.index, "caption": t.caption, "markdown": t.markdown} for t in tables[:10]],
        visual_claims=[v for v in _visual_axis(images)],
        gaps=[f"LLM normalization failed or disabled: {reason}"],
        noise_exclusion_summary={
            "policy": "fallback could not fully remove noise without LLM normalization",
            "excluded_categories": [],
        },
        quality={
            "product_page_confidence": "low",
            "evidence_completeness": "low",
            "fallback_reason": reason,
            "created_by": "deterministic_fallback",
        },
    )


def render_product_evidence_md(evidence: ProductEvidence) -> str:
    """Render normalized evidence JSON into a human-readable markdown artifact."""
    data = evidence.model_dump()
    lines: list[str] = ["# Product-only retailer evidence", ""]
    if evidence.product_focus_summary:
        lines += ["## Product focus summary", evidence.product_focus_summary.strip(), ""]

    lines += ["## Product identity", ""]
    if evidence.product_identity:
        for key, value in evidence.product_identity.items():
            if isinstance(value, dict):
                val = value.get("value", value)
                axes = value.get("evidence_axis", [])
                conf = value.get("confidence", "")
                axes_txt = f"({','.join(str(a) for a in axes)})" if axes else ""
                conf_txt = f"confidence={conf}" if conf else ""
                lines.append(f"- **{key}**: {val or '(missing)'} {axes_txt} {conf_txt}".rstrip())
            else:
                lines.append(f"- **{key}**: {value}")
        lines.append("")

    def _claims_section(title: str, claims: list[dict[str, Any]]) -> None:
        lines.extend([f"## {title}", ""])
        if not claims:
            lines.extend(["- No claim captured.", ""])
            return
        for c in claims:
            claim = c.get("claim") or c.get("value") or c.get("content") or json.dumps(c, ensure_ascii=False)
            attr = c.get("attribute") or c.get("heading") or c.get("claim_id") or "claim"
            axes = c.get("evidence_axis") or []
            conf = c.get("confidence") or ""
            refs = c.get("source_refs") or []
            suffix = f" ({','.join(axes)})" if axes else ""
            if conf:
                suffix += f" confidence={conf}"
            lines.append(f"- **{attr}**: {claim}{suffix}")
            if refs:
                lines.append(f"  - refs: {'; '.join(str(r) for r in refs[:5])}")
        lines.append("")

    _claims_section("Retailer product claims", data.get("retailer_claims", []))
    _claims_section("Structured metadata claims", data.get("structured_claims", []))
    _claims_section("Table/spec claims", data.get("table_claims", []))
    _claims_section("Visual claims", data.get("visual_claims", []))

    lines += ["## Product-only text blocks", ""]
    blocks = data.get("product_only_text_blocks") or []
    if not blocks:
        lines += ["- No product-only text blocks captured.", ""]
    else:
        for b in blocks:
            heading = b.get("heading") or "Product text"
            content = (b.get("content") or "").strip()
            axes = b.get("evidence_axis") or ["T"]
            lines += [f"### {heading} ({','.join(axes)})", content or "(empty)", ""]

    lines += ["## Discrepancies", ""]
    discrepancies = data.get("discrepancies") or []
    if discrepancies:
        for d in discrepancies:
            lines.append(f"- {json.dumps(d, ensure_ascii=False)}")
    else:
        lines.append("- No discrepancy detected from supplied evidence.")
    lines.append("")

    lines += ["## Gaps", ""]
    gaps = data.get("gaps") or []
    if gaps:
        for g in gaps:
            lines.append(f"- {g}")
    else:
        lines.append("- No explicit gap reported by the evidence normalizer.")
    lines.append("")

    lines += ["## Noise exclusion summary", ""]
    noise = data.get("noise_exclusion_summary") or {}
    lines.append("```json")
    lines.append(json.dumps(noise, ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append("")

    lines += ["## Quality", ""]
    lines.append("```json")
    lines.append(json.dumps(data.get("quality") or {}, ensure_ascii=False, indent=2))
    lines.append("```")
    return "\n".join(lines).strip() + "\n"


def build_noise_report(evidence: ProductEvidence) -> dict[str, Any]:
    """Machine-readable report proving noisy content was intentionally excluded."""
    noise = evidence.noise_exclusion_summary or {}
    return {
        "policy": noise.get("policy", "product-only artifact; noisy page content excluded"),
        "excluded_categories": noise.get("excluded_categories", []),
        "notes": noise.get("notes", []),
        "raw_noise_text_persisted": False,
        "purpose": "documents exclusion policy without storing noisy raw page content in the main artifact",
    }


def synthesize_claims_md_from_evidence(evidence: ProductEvidence) -> str:
    """Use LLM to turn normalized evidence JSON into the final claims.md."""
    from .services.llm import get_llm_service

    user = (
        "Create claims.md from this normalized product-only evidence JSON. "
        "Do not add new facts. Do not include navigation/footer/recommendations/noise.\n\n"
        f"```json\n{json.dumps(evidence.model_dump(), ensure_ascii=False, indent=2)}\n```"
    )
    resp = get_llm_service().predict(
        user,
        system_prompt=P.CLAIMS_MD.system,
        max_tokens=8192,
        temperature=0.0,
        purpose=P.CLAIMS_MD.name,
    )
    return (resp.content or "").strip()


__all__ = [
    "plan_next_actions",
    "normalize_product_evidence",
    "deterministic_product_evidence",
    "render_product_evidence_md",
    "build_noise_report",
    "synthesize_claims_md_from_evidence",
]
