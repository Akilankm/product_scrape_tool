"""LLM-driven same-page planning and product-only evidence normalization."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from .full_scraper import FullPage
from .log import logger
from .models import AgentPlan, ImageRef, ProductInputContext, ProductEvidence, TableRef, UpstreamEvidenceBundle
from .url_analysis import URLAnalysis
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


def page_observation_summary(page: FullPage, input_context: ProductInputContext, product_hint: str, url_analysis: URLAnalysis | None = None) -> str:
    """Compact planner context — enough for gap detection, not full final evidence."""
    head = {
        "requested_url": page.url,
        "final_url": page.final_url or page.url,
        "title": page.title,
        "description": page.description,
        "canonical_url": page.canonical_url,
        "profiles_merged": page.profiles_merged,
        "access": {
            "access_status": page.access_status,
            "access_issue_type": page.access_issue_type,
            "access_issue_reason": page.access_issue_reason,
            "geo_restricted": page.geo_restricted,
            "proxy_used": page.proxy_used,
            "proxy_source": page.proxy_source,
            "access_attempts": page.access_attempts,
        },
        "counts": {
            "markdown_chars": len(page.raw_markdown or ""),
            "html_chars": len(page.raw_html or ""),
            "image_candidates": len(page.images),
            "tables_html": len(page.tables_html),
            "json_ld_blocks": len(page.json_ld),
        },
        "primary_input": {"product_url": page.url},
        "url_analysis": url_analysis.model_dump() if url_analysis else {},
        "supporting_context": input_context.model_dump(),
        "input_context": input_context.model_dump(),
        "context_policy": "product_url is primary; optional fields are supporting planning/validation/routing context only",
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


def plan_next_actions(page: FullPage, input_context: ProductInputContext, product_hint: str, url_analysis: URLAnalysis | None = None) -> AgentPlan:
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
        f"{page_observation_summary(page, input_context, product_hint, url_analysis)}"
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
        "access_status": page.access_status,
        "access_issue_type": page.access_issue_type,
        "access_issue_reason": page.access_issue_reason,
        "geo_restricted": page.geo_restricted,
        "proxy_used": page.proxy_used,
        "proxy_source": page.proxy_source,
        "access_attempts": page.access_attempts,
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



def _upstream_axis(upstream_evidence: UpstreamEvidenceBundle) -> dict[str, Any]:
    """Evidence axis A: caller-supplied indexed/search/AI evidence.

    The scraper does not fetch this. It is used only when the upstream search
    system has already produced evidence. Claims based on this axis must be
    tagged as A and cannot be represented as browser-visible retailer text.
    """
    if not upstream_evidence or not upstream_evidence.has_any():
        return {"present": False}
    data = upstream_evidence.compact(max_chars=60_000)
    data["present"] = True
    data["axis"] = "A"
    data["policy"] = (
        "Use as grounded indexed/upstream evidence only. Do not present A-axis "
        "claims as browser-rendered retailer claims unless supported by B/T/P/S/D/V."
    )
    return data


def evidence_axes_from_product_evidence(evidence: ProductEvidence) -> list[str]:
    """Collect evidence axes used in normalized product evidence."""
    axes: set[str] = set()

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for k, val in v.items():
                if k in {"evidence_axis", "evidence_axes"} and isinstance(val, list):
                    axes.update(str(x) for x in val if x)
                else:
                    walk(val)
        elif isinstance(v, list):
            for item in v:
                walk(item)

    walk(evidence.model_dump())
    return sorted(axes)


def deterministic_product_details_recovered(evidence: ProductEvidence) -> bool:
    """Conservative success predicate for artifact creation."""
    if not evidence:
        return False
    data = evidence.model_dump()
    if (
        data.get("retailer_claims")
        or data.get("structured_claims")
        or data.get("table_claims")
        or data.get("visual_claims")
        or data.get("upstream_indexed_claims")
    ):
        return True
    ident = data.get("product_identity") or {}
    for val in ident.values():
        if isinstance(val, dict) and str(val.get("value", "")).strip():
            return True
        if isinstance(val, str) and val.strip():
            return True
    blocks = data.get("product_only_text_blocks") or []
    return any((b.get("content") or "").strip() for b in blocks if isinstance(b, dict))

def normalize_product_evidence(
    *,
    page: FullPage,
    tables: list[TableRef],
    images: list[ImageRef],
    input_context: ProductInputContext,
    product_hint: str,
    upstream_evidence: UpstreamEvidenceBundle | None,
    scrape_id: str,
    url_analysis: URLAnalysis | None = None,
    proxy_plan: dict[str, Any] | None = None,
) -> ProductEvidence:
    """Produce the main noise-free product evidence JSON using the LLM."""
    from .services.llm import get_llm_service

    expected_schema = {
        "url_first_trace": {
            "primary_product_url": "",
            "url_decomposition_summary": "domain/slug/product-id signals used for planning",
            "supporting_context_role": "main_text/EAN/retailer/country are validation/planning context only",
            "context_conflicts": [],
        },
        "product_focus_summary": "1-3 sentence retailer-claim summary, no guesses",
        "product_identity": {
            "product_name": {"value": "", "evidence_axis": ["B", "T", "P", "A", "S"], "source_refs": [], "confidence": "high|medium|low|missing"},
            "brand": {"value": "", "evidence_axis": ["B", "T", "P", "A", "V", "S", "D"], "source_refs": [], "confidence": "high|medium|low|missing"},
            "ean_gtin": {"value": "", "evidence_axis": ["S", "D", "T", "A", "I"], "source_refs": [], "confidence": "high|medium|low|missing"},
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
        "upstream_indexed_claims": [],
        "discrepancies": [],
        "gaps": [],
        "noise_exclusion_summary": {
            "policy": "product-only; excluded unrelated page/site content",
            "excluded_categories": ["navigation", "footer", "recommendations", "cookie text", "generic shipping/payment boilerplate", "ads", "unrelated products"],
            "notes": []
        },
        "quality": {
            "access_status": "accessible|geo_restricted|access_denied|bot_challenge|rate_limited|server_error|fetch_error|unknown",
            "geo_restricted": False,
            "proxy_used": False,
            "product_page_confidence": "high|medium|low",
            "evidence_completeness": "high|medium|low",
            "has_text_evidence": True,
            "has_structured_evidence": True,
            "has_table_evidence": True,
            "has_visual_evidence": True,
            "has_upstream_indexed_evidence": False,
            "browser_visible": True,
            "product_details_recovered": True,
            "recovery_status": "browser_primary|proxy_primary|metadata_recovery|upstream_recovery|mixed_recovery|insufficient_evidence",
            "agentic_iterations_used": 0,
        },
    }

    payload = {
        "scrape_id": scrape_id,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "primary_input": {"product_url": page.url},
        "url_analysis": url_analysis.model_dump() if url_analysis else {},
        "proxy_plan": proxy_plan or {},
        "supporting_context_policy": "URL is the primary input. Optional fields are not product truth; use them only for decision trace, validation, relevance, locale/proxy routing.",
        "input_context": input_context.model_dump(),
        "product_hint": product_hint,
        "axis_S_structured": _structured_axis(page),
        "axis_D_tables": _tables_axis(tables),
        "axis_V_visual": _visual_axis(images),
        "axis_T_rendered_markdown": truncate_text(page.raw_markdown or "", _MD_EVIDENCE_CHARS),
        "axis_A_upstream_indexed_evidence": _upstream_axis(upstream_evidence or UpstreamEvidenceBundle()),
        # Small HTML signal helps when text markdown misses alt/data attributes; still not a raw dump.
        "html_signal_sample": truncate_text(page.raw_html or "", _HTML_SIGNAL_CHARS),
    }
    user = (
        "Build the complete URL-first, product-only retailer evidence JSON. Remove noise; do not summarize noisy content. "
        "Treat product_url as the primary anchor; use main_text, EAN, retailer_name, country_code only as supporting trace/validation/routing context. "
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
    upstream_evidence: UpstreamEvidenceBundle | None,
    reason: str,
    url_analysis: URLAnalysis | None = None,
    proxy_plan: dict[str, Any] | None = None,
) -> ProductEvidence:
    """Safe fallback when the LLM is unavailable; does not pretend to be complete."""
    return ProductEvidence(
        url_first_trace={
            "primary_product_url": page.url,
            "url_analysis": url_analysis.model_dump() if url_analysis else {},
            "proxy_plan": proxy_plan or {},
            "supporting_context_role": "Optional inputs are trace/validation/routing context only; no product fact is invented from them.",
        },
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
        product_only_text_blocks=(
            [{
                "heading": "Upstream evidence text — deterministic recovery",
                "content": truncate_text((upstream_evidence.ai_mode_evidence or "") if upstream_evidence else "", 12_000),
                "evidence_axis": ["A"],
            }]
            if upstream_evidence and (upstream_evidence.ai_mode_evidence or "").strip()
            else []
        ),
        structured_claims=[{"source": "metadata/json_ld", "value": _structured_axis(page)}],
        upstream_indexed_claims=[{"source": "upstream_evidence", "value": _upstream_axis(upstream_evidence or UpstreamEvidenceBundle())}] if (upstream_evidence and upstream_evidence.has_any()) else [],
        table_claims=[{"table_index": t.index, "caption": t.caption, "markdown": t.markdown} for t in tables[:10]],
        visual_claims=[v for v in _visual_axis(images)],
        gaps=[f"LLM normalization failed or disabled: {reason}"],
        noise_exclusion_summary={
            "policy": "strict product-only fallback; raw rendered page text is not emitted because it may contain noise",
            "excluded_categories": ["navigation", "footer", "recommendations", "cookie text", "generic shipping/payment boilerplate", "ads", "unrelated products"],
        },
        quality={
            "access_status": page.access_status,
            "access_issue_type": page.access_issue_type,
            "access_issue_reason": page.access_issue_reason,
            "geo_restricted": page.geo_restricted,
            "proxy_used": page.proxy_used,
            "proxy_source": page.proxy_source,
            "url_analysis": url_analysis.model_dump() if url_analysis else {},
            "proxy_plan": proxy_plan or {},
            "url_first_policy": "product_url is primary; optional context is supporting only",
            "browser_visible": bool(page.success and (page.raw_markdown or page.raw_html) and page.access_status == "accessible"),
            "has_upstream_indexed_evidence": bool(upstream_evidence and upstream_evidence.has_any()),
            "product_details_recovered": bool(upstream_evidence and upstream_evidence.has_any()) or bool(page.title or page.json_ld or page.raw_markdown),
            "recovery_status": "upstream_recovery" if (upstream_evidence and upstream_evidence.has_any() and not page.success) else "deterministic_fallback",
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
    _claims_section("Upstream indexed/search/AI claims", data.get("upstream_indexed_claims", []))

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



def build_evidence_recovery_report(
    *,
    result: Any,
    evidence: ProductEvidence | None,
    upstream_evidence: UpstreamEvidenceBundle,
    page: FullPage,
) -> dict[str, Any]:
    """Explain how product details were recovered when browser access is weak/blocked."""
    axes = evidence_axes_from_product_evidence(evidence) if evidence else []
    recovered = deterministic_product_details_recovered(evidence) if evidence else False
    browser_visible = bool(page.success and (page.raw_markdown or page.raw_html) and page.access_status == "accessible")
    recovery_sources: list[str] = []
    if browser_visible:
        recovery_sources.append("browser_rendered_page")
    if page.proxy_used:
        recovery_sources.append("target_country_proxy")
    if page.raw_markdown:
        recovery_sources.append("rendered_markdown")
    if page.raw_html:
        recovery_sources.append("html_signal")
    if page.og or page.product_meta:
        recovery_sources.append("metadata_meta_tags")
    if page.json_ld:
        recovery_sources.append("json_ld")
    if page.images:
        recovery_sources.append("image_urls_or_gallery")
    if upstream_evidence and upstream_evidence.has_any():
        recovery_sources.append("upstream_indexed_search_ai_evidence")

    status = "recovered" if recovered else "insufficient_evidence"
    if browser_visible and recovered:
        status = "browser_primary"
    elif page.proxy_used and recovered:
        status = "proxy_primary"
    elif upstream_evidence and upstream_evidence.has_any() and recovered:
        status = "upstream_recovery" if not browser_visible else "mixed_recovery"
    elif (page.json_ld or page.og or page.product_meta) and recovered:
        status = "metadata_recovery"

    return {
        "browser_visible": browser_visible,
        "access_status": page.access_status,
        "access_issue_type": page.access_issue_type,
        "access_issue_reason": page.access_issue_reason,
        "geo_restricted": page.geo_restricted,
        "proxy_attempted": any(a.get("proxy_used") for a in (page.access_attempts or [])),
        "proxy_used": page.proxy_used,
        "proxy_source": page.proxy_source,
        "product_details_recovered": recovered,
        "recovery_status": status,
        "recovery_sources": recovery_sources,
        "evidence_axes_used": axes,
        "upstream_evidence_present": bool(upstream_evidence and upstream_evidence.has_any()),
        "policy": (
            "Browser access failure is not treated as product absence. Product facts are emitted only when "
            "grounded in supplied evidence and tagged by axis. A-axis claims come from caller-supplied "
            "upstream/indexed evidence, not from new search performed by this scraper."
        ),
        "notes": [
            "No external search is performed inside this agent.",
            "Noisy navigation/footer/recommendation content is excluded from product artifacts.",
            "If product_details_recovered=false, the agent had insufficient grounded evidence and did not invent facts.",
        ],
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
    "evidence_axes_from_product_evidence",
    "deterministic_product_details_recovered",
    "build_evidence_recovery_report",
    "render_product_evidence_md",
    "build_noise_report",
    "synthesize_claims_md_from_evidence",
]
