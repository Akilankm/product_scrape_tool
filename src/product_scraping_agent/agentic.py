"""LLM-driven same-page planning and product-only evidence normalization."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from .full_scraper import FullPage
from .log import logger
from .models import AgentPlan, ImageRef, ProductInputContext, ProductEvidence, TableRef, UpstreamEvidenceBundle
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
) -> ProductEvidence:
    """Produce the main noise-free product evidence JSON using the LLM."""
    from .services.llm import get_llm_service

    expected_schema = {
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
    upstream_evidence: UpstreamEvidenceBundle | None,
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



def _evidence_text_blob(evidence: ProductEvidence | None) -> str:
    if evidence is None:
        return ""
    try:
        return json.dumps(evidence.model_dump(), ensure_ascii=False).lower()
    except Exception:
        return str(evidence).lower()


def _has_any_term(blob: str, terms: list[str]) -> bool:
    return any(term in blob for term in terms)


def build_artifact_quality_report(
    *,
    evidence: ProductEvidence,
    result: Any,
    page: FullPage,
    tables: list[TableRef],
    images: list[ImageRef],
    input_context: ProductInputContext,
    upstream_evidence: UpstreamEvidenceBundle,
) -> dict[str, Any]:
    """Deterministic quality gate for the final product-only artifact.

    The LLM creates the rich artifact; this gate audits whether the artifact is
    strong enough to hand to downstream product coding. It never invents facts —
    it checks presence, evidence axes, and capture health.
    """
    blob = _evidence_text_blob(evidence)
    identity = evidence.product_identity or {}
    claims_count = (
        len(evidence.retailer_claims or [])
        + len(evidence.structured_claims or [])
        + len(evidence.table_claims or [])
        + len(evidence.visual_claims or [])
        + len(getattr(evidence, "upstream_indexed_claims", []) or [])
    )
    product_text_chars = sum(len(str((b or {}).get("content", ""))) for b in (evidence.product_only_text_blocks or []))
    downloaded_images = [img for img in images if img.local_path]
    described_images = [img for img in images if img.description]
    download_403 = [img for img in images if "403" in (img.error or "")]
    download_errors = [img for img in images if img.error]
    axes = evidence_axes_from_product_evidence(evidence)

    checks = {
        "has_product_name_or_title": bool(
            _has_any_term(blob, ["product_name", "product name", "name", "title"])
            or bool(page.title)
            or bool(input_context.main_text)
        ),
        "has_brand_signal": bool(_has_any_term(blob, ["brand", "manufacturer", "publisher"])) ,
        "has_identifier_signal": bool(
            bool(input_context.ean)
            or _has_any_term(blob, ["ean", "gtin", "barcode", "isbn", "sku", "mpn", "article", "artikeldetails"])
        ),
        "has_retailer_url": bool(result.final_url or page.final_url or page.url),
        "has_product_text": bool(product_text_chars >= 80 or evidence.retailer_claims or page.raw_markdown),
        "has_visual_evidence": bool(described_images),
        "has_table_or_structured_evidence": bool(tables or page.json_ld or page.og or page.product_meta or evidence.structured_claims or evidence.table_claims),
        "has_evidence_axes": bool(axes),
        "has_gap_reporting": isinstance(evidence.gaps, list),
        "noise_exclusion_documented": bool(evidence.noise_exclusion_summary),
        "browser_access_ok_or_recovered": bool(result.browser_visible or result.product_details_recovered or upstream_evidence.has_any()),
    }

    missing: list[str] = []
    if not checks["has_product_name_or_title"]:
        missing.append("product_name_or_title")
    if not checks["has_brand_signal"]:
        missing.append("brand_or_manufacturer_signal")
    if not checks["has_retailer_url"]:
        missing.append("retailer_url")
    if not checks["has_product_text"] and not checks["has_table_or_structured_evidence"] and not checks["has_visual_evidence"]:
        missing.append("product_evidence_content")
    if not checks["has_evidence_axes"]:
        missing.append("evidence_axis_tags")

    warnings: list[str] = []
    if download_403:
        warnings.append(f"{len(download_403)} image candidate(s) failed with HTTP 403; CDN recovery may be partial")
    if download_errors and len(downloaded_images) < max(3, len(images) // 4):
        warnings.append("image download success rate is low")
    if not described_images:
        warnings.append("no vision-described product image retained")
    if not tables and not page.json_ld:
        warnings.append("no table or JSON-LD product data captured")
    if result.access_status != "accessible":
        warnings.append(f"browser access status is {result.access_status}; evidence recovery mode used")

    # Score favours multiple independent axes over volume. This is deliberately
    # conservative because the artifact is used downstream for product coding.
    score = 0
    score += 20 if checks["has_product_name_or_title"] else 0
    score += 15 if checks["has_brand_signal"] else 0
    score += 10 if checks["has_identifier_signal"] else 0
    score += 10 if checks["has_retailer_url"] else 0
    score += 15 if checks["has_product_text"] else 0
    score += 10 if checks["has_table_or_structured_evidence"] else 0
    score += 10 if checks["has_visual_evidence"] else 0
    score += 10 if checks["has_evidence_axes"] else 0
    if claims_count >= 5:
        score += 5
    if len(warnings) >= 3:
        score -= 10
    score = max(0, min(100, score))

    if missing:
        artifact_quality = "partial" if score >= 45 else "insufficient"
    elif score >= 85 and len(warnings) <= 1:
        artifact_quality = "strong"
    elif score >= 65:
        artifact_quality = "usable"
    else:
        artifact_quality = "partial"

    requires_review = artifact_quality in {"partial", "insufficient"} or bool(missing)
    recommended_followups: list[str] = []
    if "product_name_or_title" in missing or "product_evidence_content" in missing:
        recommended_followups.append("rerun with max_agent_iterations>=3 and write_raw_debug=true for audit")
    if not described_images:
        recommended_followups.append("check PCA_LLM_VISION_ENABLED and gateway vision permissions")
    if download_403:
        recommended_followups.append("enable/verify image CDN retry settings and referer/cookie/proxy configuration")
    if result.access_status != "accessible":
        recommended_followups.append("configure authorised target-country proxy or pass upstream AI/search evidence")
    if not input_context.main_text and not input_context.ean:
        recommended_followups.append("provide main_text and/or EAN to strengthen identity validation")

    return {
        "artifact_quality": artifact_quality,
        "quality_score": score,
        "requires_manual_review": requires_review,
        "missing_critical_fields": missing,
        "warnings": warnings,
        "checks": checks,
        "evidence_axes_used": axes,
        "counts": {
            "retailer_claims": len(evidence.retailer_claims or []),
            "structured_claims": len(evidence.structured_claims or []),
            "table_claims": len(evidence.table_claims or []),
            "visual_claims": len(evidence.visual_claims or []),
            "product_only_text_blocks": len(evidence.product_only_text_blocks or []),
            "product_only_text_chars": product_text_chars,
            "downloaded_images": len(downloaded_images),
            "vision_described_images": len(described_images),
            "image_download_errors": len(download_errors),
            "image_http_403_errors": len(download_403),
            "tables": len(tables),
            "json_ld_blocks": len(page.json_ld or []),
        },
        "access": {
            "access_status": result.access_status,
            "browser_visible": result.browser_visible,
            "product_details_recovered": result.product_details_recovered,
            "recovery_status": result.recovery_status,
            "proxy_used": result.proxy_used,
            "geo_restricted": result.geo_restricted,
        },
        "recommended_followups": list(dict.fromkeys(recommended_followups)),
        "policy": "Quality gate audits artifact completeness only; it does not add facts or use external knowledge.",
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
    "build_artifact_quality_report",
    "render_product_evidence_md",
    "build_noise_report",
    "synthesize_claims_md_from_evidence",
]
