"""LLM-driven same-page planning and product-only evidence normalization."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlparse

from .full_scraper import FullPage
from .log import logger
from .models import AgentPlan, ImageRef, ProductInputContext, ProductEvidence, SourceAlignmentContext, TableRef, UpstreamEvidenceBundle
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


def page_observation_summary(page: FullPage, input_context: ProductInputContext, source_alignment: SourceAlignmentContext, product_hint: str) -> str:
    """Compact planner context — enough for gap detection, not full final evidence."""
    head = {
        "requested_url": page.url,
        "final_url": page.final_url or page.url,
        "title": page.title,
        "description": page.description,
        "canonical_url": page.canonical_url,
        "profiles_merged": page.profiles_merged,
        "source_alignment": source_alignment.model_report(),
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
        "capture_health": page_capture_health(page),
        "source_alignment": source_alignment.model_report(),
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


def plan_next_actions(page: FullPage, input_context: ProductInputContext, source_alignment: SourceAlignmentContext, product_hint: str) -> AgentPlan:
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
        f"{page_observation_summary(page, input_context, source_alignment, product_hint)}"
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



def derive_url_evidence(page: FullPage | None, product_url: str = "") -> dict[str, Any]:
    """Axis U: product evidence derivable from the provided URL itself.

    This is not external search. It preserves the provided fallback/primary URL as
    evidence so the artifact can still be created when browser rendering returns
    a weak shell page. URL-derived values are provenance/identity hints, not
    retailer-rendered page claims.
    """
    url = product_url or (page.url if page else "") or (page.final_url if page else "")
    final_url = (page.final_url if page else "") or url
    parsed = urlparse(final_url or url)
    path = unquote(parsed.path or "")
    segments = [s for s in re.split(r"[/_\-]+", path.strip("/")) if s]
    sku_like: list[str] = []
    for seg in re.split(r"[/&?=#._\-]+", final_url):
        token = seg.strip()
        if not token:
            continue
        # Generic identifier heuristic: ASIN-like, long numeric, or alpha-num SKU.
        if re.fullmatch(r"[A-Z0-9]{8,14}", token, flags=re.I) or re.fullmatch(r"\d{8,14}", token):
            if token not in sku_like:
                sku_like.append(token)
    title_tokens = [s for s in segments if not re.fullmatch(r"[A-Z0-9]{8,14}|\d{8,14}", s, flags=re.I)]
    title_hint = " ".join(title_tokens[:14]).strip()
    return {
        "present": bool(final_url or url),
        "axis": "U",
        "source_url": final_url or url,
        "domain": parsed.netloc,
        "path": path,
        "path_segments": segments[:30],
        "url_title_hint": title_hint,
        "url_identifiers": sku_like[:12],
        "policy": (
            "U-axis facts come from the supplied URL string only. Use them as provenance/identity hints; "
            "do not present them as browser-rendered retailer page text unless supported by T/S/D/V."
        ),
    }


def page_capture_health(page: FullPage) -> dict[str, Any]:
    """Classify whether the browser captured a real product page.

    If full_scraper's multi-profile scorer populated capture_score/grade, use
    that as the source of truth. Otherwise fall back to the older heuristics.
    """
    md = page.raw_markdown or ""
    html = page.raw_html or ""
    title = (page.title or "").strip()
    text = "\n".join([title, md, html[:20_000]]).lower()
    md_chars = len(md)
    html_chars = len(html)
    product_terms = [
        "product", "brand", "manufacturer", "ean", "gtin", "sku", "mpn", "asin",
        "price", "availability", "description", "details", "features", "specification",
        "item model", "model number", "age", "material", "dimensions", "pieces", "package",
    ]
    block_terms = [
        "captcha", "robot check", "not a robot", "automated access", "enable javascript and cookies",
        "enter the characters", "validatecaptcha", "access denied", "request blocked",
        "verifica tu identidad", "verify your identity", "unusual traffic",
    ]
    product_signal_count = sum(1 for term in product_terms if term in text)
    block_signal_count = sum(1 for term in block_terms if term in text)
    generic_title = title.lower() in {"amazon.com", "amazon", "access denied", "robot check", "captcha", "verifica tu identidad"} or not title
    structured_count = len(page.json_ld or []) + len(page.tables_html or []) + len(page.og or {}) + len(page.product_meta or {})
    image_count = len(page.images or [])

    weak_reasons: list[str] = []
    if page.access_status != "accessible":
        weak_reasons.append(f"access_status={page.access_status}")
    if block_signal_count:
        weak_reasons.append("soft_block_terms_detected")
    if md_chars < 1200 and html_chars < 8000 and structured_count == 0:
        weak_reasons.append("very_low_text_and_no_structured_evidence")
    if generic_title and md_chars < 2500:
        weak_reasons.append("generic_or_missing_title_with_low_text")
    if product_signal_count < 2 and md_chars < 2500 and structured_count == 0:
        weak_reasons.append("few_product_signals")

    capture_score = int(getattr(page, "capture_score", 0) or 0)
    capture_grade = str(getattr(page, "capture_grade", "") or "")
    scorer_reasons = list(getattr(page, "weak_capture_reasons", []) or [])
    real_scrape_evidence = bool(getattr(page, "real_scrape_evidence", False))
    if scorer_reasons:
        weak_reasons = list(dict.fromkeys([*weak_reasons, *scorer_reasons]))
    if capture_score:
        browser_product_visible = bool(page.success and page.access_status == "accessible" and real_scrape_evidence and capture_grade in {"strong", "usable"})
    else:
        browser_product_visible = bool(page.success and page.access_status == "accessible" and not weak_reasons)
    status = "product_visible" if browser_product_visible else ("weak_capture" if page.success or html or md else "not_captured")
    return {
        "browser_product_visible": browser_product_visible,
        "capture_status": status,
        "weak_capture": not browser_product_visible,
        "weak_reasons": list(dict.fromkeys(weak_reasons)),
        "capture_score": capture_score,
        "capture_grade": capture_grade or "not_evaluated",
        "capture_profile_used": getattr(page, "capture_profile_used", "") or getattr(page, "fetch_profile", ""),
        "capture_profiles_attempted": list(getattr(page, "capture_profiles_attempted", []) or []),
        "real_scrape_evidence": real_scrape_evidence,
        "markdown_chars": md_chars,
        "html_chars": html_chars,
        "title": title,
        "generic_title": generic_title,
        "product_signal_count": product_signal_count,
        "block_signal_count": block_signal_count,
        "structured_signal_count": structured_count,
        "image_candidate_count": image_count,
    }

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
        "capture_profile_used": page.capture_profile_used or page.fetch_profile,
        "capture_profiles_attempted": page.capture_profiles_attempted,
        "capture_profile_scores": page.capture_profile_scores,
        "capture_score": page.capture_score,
        "capture_grade": page.capture_grade,
        "real_scrape_evidence": page.real_scrape_evidence,
        "weak_capture_reasons": page.weak_capture_reasons,
        "access_status": page.access_status,
        "access_issue_type": page.access_issue_type,
        "access_issue_reason": page.access_issue_reason,
        "geo_restricted": page.geo_restricted,
        "proxy_used": page.proxy_used,
        "proxy_source": page.proxy_source,
        "access_attempts": page.access_attempts,
        "capture_health": page_capture_health(page),
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
    source_alignment: SourceAlignmentContext,
    product_hint: str,
    upstream_evidence: UpstreamEvidenceBundle | None,
    scrape_id: str,
) -> ProductEvidence:
    """Produce the main noise-free product evidence JSON using the LLM."""
    from .services.llm import get_llm_service

    expected_schema = {
        "product_focus_summary": "1-3 sentence product/source summary, no guesses; include if browser capture is weak",
        "source_alignment": {
            "alignment_status": "primary_requested_source|fallback_source_used|source_context_mismatch|not_declared",
            "requested_context": {},
            "scraped_source": {},
            "product_facts_transfer_allowed": True,
            "requested_retailer_claims_allowed": False,
            "source_specific_claim_scope": "scraped_source_only|requested_retailer_and_country",
            "business_interpretation": "how to interpret this source for product coding"
        },
        "product_identity": {
            "product_name": {"value": "", "evidence_axis": ["B", "T", "P", "A", "S", "I", "U"], "source_refs": [], "confidence": "high|medium|low|missing"},
            "brand": {"value": "", "evidence_axis": ["B", "T", "P", "A", "V", "S", "D"], "source_refs": [], "confidence": "high|medium|low|missing"},
            "ean_gtin": {"value": "", "evidence_axis": ["S", "D", "T", "A", "I", "U"], "source_refs": [], "confidence": "high|medium|low|missing"},
            "sku_mpn": {"value": "", "evidence_axis": [], "source_refs": [], "confidence": "high|medium|low|missing"},
            "manufacturer": {"value": "", "evidence_axis": [], "source_refs": [], "confidence": "high|medium|low|missing"},
            "retailer": {"value": "", "evidence_axis": ["I", "S"], "source_refs": [], "confidence": "high|medium|low|missing"},
        },
        "retailer_claims": [
            {
                "claim_id": "C001",
                "claim_type": "product_level",
                "attribute": "age_range | piece_count | material | contents | features | category | dimensions | battery | warning | etc",
                "value": "exact value as supported by evidence",
                "claim": "complete grounded claim sentence",
                "evidence_axis": ["T"],
                "source_refs": ["T: short quote or section label"],
                "confidence": "high|medium|low",
                "claim_scope": "product_level_transferable",
                "notes": "",
            }
        ],
        "source_specific_claims": [
            {
                "claim_id": "S001",
                "claim_type": "source_specific",
                "attribute": "price | availability | delivery | seller | marketplace_terms | rating | shipping",
                "value": "exact source-specific value if captured",
                "claim": "claim scoped only to scraped source unless alignment is primary",
                "evidence_axis": ["T", "S", "D"],
                "claim_scope": "scraped_source_only|requested_retailer_and_country",
                "transfer_to_requested_retailer_allowed": False,
                "confidence": "high|medium|low",
                "source_refs": []
            }
        ],
        "product_only_text_blocks": [
            {"heading": "Product description", "content": "clean product-only text, no nav/footer/recommendations", "evidence_axis": ["T"]}
        ],
        "structured_claims": [],
        "table_claims": [],
        "visual_claims": [],
        "upstream_indexed_claims": [],
        "url_derived_claims": [],
        "input_context_claims": [],
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
        "axis_I_input_context": {"present": input_context.has_any(), "axis": "I", "value": input_context.model_dump(), "policy": "Caller-supplied product identity context; valid provenance but not browser-rendered retailer page text."},
        "input_context": input_context.model_dump(),
        "axis_U_url_derived_evidence": derive_url_evidence(page, page.final_url or page.url),
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
    source_alignment: SourceAlignmentContext | None = None,
    product_hint: str = "",
    upstream_evidence: UpstreamEvidenceBundle | None = None,
    reason: str = "",
) -> ProductEvidence:
    """Safe fallback when the LLM is unavailable or the browser capture is weak.

    This still creates the artifact from the supplied URL + user/business inputs.
    It does not claim those facts were scraped from rendered page text unless T/S/D/V
    evidence supports them.
    """
    source_alignment = source_alignment or SourceAlignmentContext(
        requested_retailer_name=input_context.retailer_name,
        requested_country_code=input_context.country_code,
        source_url_role="unknown",
    )
    upstream_evidence = upstream_evidence or UpstreamEvidenceBundle()
    url_axis = derive_url_evidence(page, page.final_url or page.url)
    capture = page_capture_health(page)

    blocks: list[dict[str, Any]] = []
    if input_context.has_any():
        blocks.append({
            "heading": "Caller-supplied product identity context",
            "content": input_context.compact_hint(),
            "evidence_axis": ["I"],
            "claim_scope": "input_context_provenance",
        })
    if url_axis.get("url_title_hint") or url_axis.get("url_identifiers"):
        blocks.append({
            "heading": "URL-derived product/source hints",
            "content": json.dumps({
                "url_title_hint": url_axis.get("url_title_hint"),
                "url_identifiers": url_axis.get("url_identifiers"),
                "source_url": url_axis.get("source_url"),
                "domain": url_axis.get("domain"),
            }, ensure_ascii=False),
            "evidence_axis": ["U"],
            "claim_scope": "url_provenance_not_browser_text",
        })
    if upstream_evidence and (upstream_evidence.ai_mode_evidence or "").strip():
        blocks.append({
            "heading": "Upstream evidence text — deterministic recovery",
            "content": truncate_text(upstream_evidence.ai_mode_evidence or "", 12_000),
            "evidence_axis": ["A"],
        })

    return ProductEvidence(
        product_focus_summary=(
            "Artifact created from the supplied product URL and provided product identity context. "
            "Browser/page capture may be weak; every retained fact is tagged by evidence axis."
        ),
        source_alignment=source_alignment.model_report(),
        product_identity={
            "product_name": {
                "value": input_context.main_text or url_axis.get("url_title_hint", ""),
                "evidence_axis": ["I"] if input_context.main_text else (["U"] if url_axis.get("url_title_hint") else []),
                "source_refs": ["input:main_text"] if input_context.main_text else (["url:path"] if url_axis.get("url_title_hint") else []),
                "confidence": "high" if input_context.main_text else ("low" if url_axis.get("url_title_hint") else "missing"),
            },
            "ean_gtin": {
                "value": input_context.ean,
                "evidence_axis": ["I"] if input_context.ean else [],
                "source_refs": ["input:ean"] if input_context.ean else [],
                "confidence": "high" if input_context.ean else "missing",
            },
            "source_url": {
                "value": url_axis.get("source_url", page.final_url or page.url),
                "evidence_axis": ["U"],
                "source_refs": ["input:product_url"],
                "confidence": "high",
            },
            "source_url_identifiers": {
                "value": url_axis.get("url_identifiers", []),
                "evidence_axis": ["U"] if url_axis.get("url_identifiers") else [],
                "source_refs": ["url:path_or_query"] if url_axis.get("url_identifiers") else [],
                "confidence": "medium" if url_axis.get("url_identifiers") else "missing",
            },
            "page_title": {
                "value": page.title,
                "evidence_axis": ["S", "T"] if page.title else [],
                "source_refs": ["metadata:title"] if page.title else [],
                "confidence": "medium" if page.title and not capture.get("weak_capture") else ("low" if page.title else "missing"),
            },
            "canonical_url": {
                "value": page.canonical_url,
                "evidence_axis": ["S"] if page.canonical_url else [],
                "source_refs": ["metadata:canonical"] if page.canonical_url else [],
                "confidence": "medium" if page.canonical_url else "missing",
            },
            "requested_retailer": {
                "value": input_context.retailer_name,
                "evidence_axis": ["I"] if input_context.retailer_name else [],
                "source_refs": ["input:requested_retailer_name"] if input_context.retailer_name else [],
                "confidence": "high" if input_context.retailer_name else "missing",
            },
            "requested_country": {
                "value": input_context.country_code,
                "evidence_axis": ["I"] if input_context.country_code else [],
                "source_refs": ["input:requested_country_code"] if input_context.country_code else [],
                "confidence": "high" if input_context.country_code else "missing",
            },
        },
        retailer_claims=[],
        source_specific_claims=[],
        product_only_text_blocks=blocks,
        structured_claims=[{"source": "metadata/json_ld", "value": _structured_axis(page), "evidence_axis": ["S"]}],
        upstream_indexed_claims=[{"source": "upstream_evidence", "value": _upstream_axis(upstream_evidence or UpstreamEvidenceBundle()), "evidence_axis": ["A"]}] if (upstream_evidence and upstream_evidence.has_any()) else [],
        url_derived_claims=[{"source": "product_url", "value": url_axis, "evidence_axis": ["U"], "claim_scope": "url_provenance_not_browser_text"}],
        input_context_claims=[{"source": "scrape_request", "value": input_context.model_dump(), "evidence_axis": ["I"], "claim_scope": "input_context_provenance"}] if input_context.has_any() else [],
        table_claims=[{"table_index": t.index, "caption": t.caption, "markdown": t.markdown, "evidence_axis": ["D"]} for t in tables[:10]],
        visual_claims=[v for v in _visual_axis(images)],
        gaps=[{
            "gap_id": "G001",
            "type": "normalization_or_capture_gap",
            "description": f"LLM normalization failed/disabled or browser capture was weak: {reason}",
            "capture_health": capture,
        }],
        noise_exclusion_summary={
            "policy": "strict product-only fallback; raw rendered page text is not emitted because it may contain noise",
            "excluded_categories": ["navigation", "footer", "recommendations", "cookie text", "generic shipping/payment boilerplate", "ads", "unrelated products"],
        },
        quality={
            "access_status": page.access_status,
            "access_issue_type": page.access_issue_type,
            "access_issue_reason": page.access_issue_reason,
            "capture_status": capture.get("capture_status"),
            "weak_capture_reasons": capture.get("weak_reasons"),
            "geo_restricted": page.geo_restricted,
            "proxy_used": page.proxy_used,
            "proxy_source": page.proxy_source,
            "browser_visible": bool(capture.get("browser_product_visible")),
            "has_url_derived_evidence": bool(url_axis.get("present")),
            "has_input_context_evidence": input_context.has_any(),
            "has_upstream_indexed_evidence": bool(upstream_evidence and upstream_evidence.has_any()),
            "product_details_recovered": bool(input_context.has_any() or url_axis.get("present") or (upstream_evidence and upstream_evidence.has_any()) or page.json_ld or page.og or page.product_meta),
            "recovery_status": "browser_primary" if capture.get("browser_product_visible") else "input_url_context_recovery",
            "product_page_confidence": "low" if capture.get("weak_capture") else "medium",
            "evidence_completeness": "partial" if (input_context.has_any() or url_axis.get("present")) else "low",
            "fallback_reason": reason,
            "source_alignment_status": source_alignment.alignment_status,
            "requested_retailer_claims_allowed": source_alignment.requested_retailer_claims_allowed,
            "source_specific_claim_scope": source_alignment.source_specific_claim_scope,
            "created_by": "deterministic_fallback",
        },
    )

def _cell(value: Any, *, max_len: int = 260) -> str:
    """Markdown table-safe compact cell."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace("|", "\\|")
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def _axes_cell(claim: dict[str, Any]) -> str:
    axes = claim.get("evidence_axis") or claim.get("evidence_axes") or []
    if isinstance(axes, str):
        axes = [axes]
    return ",".join(str(a) for a in axes if a)


def _confidence_cell(claim: dict[str, Any]) -> str:
    return str(claim.get("confidence") or claim.get("certainty") or "")


def _refs_cell(claim: dict[str, Any]) -> str:
    refs = claim.get("source_refs") or claim.get("sources") or claim.get("source") or []
    if isinstance(refs, str):
        refs = [refs]
    return _cell("; ".join(str(r) for r in refs[:4]), max_len=360)


def _claim_value(claim: dict[str, Any]) -> str:
    for key in ("claim", "value", "content", "description", "text"):
        val = claim.get(key)
        if val not in (None, ""):
            return _cell(val, max_len=420)
    return _cell(claim, max_len=420)


def _append_claim_table(lines: list[str], title: str, claims: list[dict[str, Any]]) -> None:
    lines.extend([f"## {title}", ""])
    if not claims:
        lines.extend(["| Status | Detail |", "|---|---|", "| Not captured | No product-specific claim found in this evidence axis. |", ""])
        return
    lines.extend([
        "| # | Attribute | Claim / Value | Evidence axes | Confidence | Source refs / notes |",
        "|---:|---|---|---|---|---|",
    ])
    for i, c in enumerate(claims, start=1):
        attr = c.get("attribute") or c.get("heading") or c.get("claim_id") or c.get("type") or "claim"
        notes = c.get("notes") or ""
        refs = _refs_cell(c)
        ref_notes = refs if refs else _cell(notes, max_len=360)
        lines.append(
            f"| {i} | {_cell(attr, max_len=120)} | {_claim_value(c)} | "
            f"{_cell(_axes_cell(c), max_len=80)} | {_cell(_confidence_cell(c), max_len=80)} | {ref_notes} |"
        )
    lines.append("")

def render_product_evidence_md(evidence: ProductEvidence) -> str:
    """Render normalized evidence JSON into a business-readable markdown artifact.

    This is intentionally table-first so stakeholders can inspect the artifact
    quickly and downstream LLMs can parse sections deterministically.
    """
    data = evidence.model_dump()
    lines: list[str] = [
        "# Product Evidence Dossier",
        "",
        "This is the clean, product-only retailer evidence view. Navigation, footer, cookie text, ads, recommendations, and unrelated products are excluded.",
        "",
    ]

    if evidence.product_focus_summary:
        lines.extend(["## Executive product summary", "", evidence.product_focus_summary.strip(), ""])

    source_align = data.get("source_alignment") or {}
    if source_align:
        lines.extend(["## Source alignment decision table", ""])
        lines.extend(["| Field | Value |", "|---|---|"])
        for key in ["alignment_status", "retailer_match", "country_match", "source_specific_claim_scope", "requested_retailer_claims_allowed", "product_facts_transfer_allowed"]:
            if key in source_align:
                lines.append(f"| {_cell(key, max_len=180)} | {_cell(source_align.get(key), max_len=420)} |")
        req = source_align.get("requested_context") or {}
        src = source_align.get("scraped_source") or {}
        lines.append(f"| requested_context | {_cell(req, max_len=520)} |")
        lines.append(f"| scraped_source | {_cell(src, max_len=520)} |")
        lines.append("")

    lines.extend(["## Identity decision table", ""])
    lines.extend(["| Field | Value | Evidence axes | Confidence | Source refs |", "|---|---|---|---|---|"])
    if evidence.product_identity:
        for key, value in evidence.product_identity.items():
            if isinstance(value, dict):
                val = value.get("value", "")
                axes = value.get("evidence_axis") or value.get("evidence_axes") or []
                conf = value.get("confidence", "")
                refs = value.get("source_refs") or []
                lines.append(
                    f"| {_cell(key, max_len=120)} | {_cell(val or '(missing)', max_len=260)} | "
                    f"{_cell(','.join(str(a) for a in axes), max_len=80)} | {_cell(conf, max_len=80)} | "
                    f"{_cell('; '.join(str(r) for r in refs[:4]), max_len=360)} |"
                )
            else:
                lines.append(f"| {_cell(key, max_len=120)} | {_cell(value, max_len=260)} |  |  |  |")
    else:
        lines.append("| Identity | Not captured |  | missing |  |")
    lines.append("")

    _append_claim_table(lines, "Product-level retailer claim decision table", data.get("retailer_claims", []))
    _append_claim_table(lines, "Source-specific commercial claim table", data.get("source_specific_claims", []))
    _append_claim_table(lines, "Structured metadata decision table", data.get("structured_claims", []))
    _append_claim_table(lines, "Table/specification decision table", data.get("table_claims", []))
    _append_claim_table(lines, "Visual evidence decision table", data.get("visual_claims", []))
    _append_claim_table(lines, "URL-derived evidence table", data.get("url_derived_claims", []))
    _append_claim_table(lines, "Input-context evidence table", data.get("input_context_claims", []))
    _append_claim_table(lines, "Upstream indexed/search/AI evidence table", data.get("upstream_indexed_claims", []))

    lines.extend(["## Product-only text evidence", ""])
    blocks = data.get("product_only_text_blocks") or []
    if not blocks:
        lines.extend(["| Status | Detail |", "|---|---|", "| Not captured | No clean product-only text block was produced. |", ""])
    else:
        lines.extend(["| # | Section | Evidence axes | Clean product text |", "|---:|---|---|---|"])
        for i, b in enumerate(blocks, start=1):
            heading = b.get("heading") or "Product text"
            content = (b.get("content") or "").strip()
            axes = b.get("evidence_axis") or ["T"]
            lines.append(
                f"| {i} | {_cell(heading, max_len=160)} | {_cell(','.join(str(a) for a in axes), max_len=80)} | {_cell(content, max_len=700)} |"
            )
        lines.append("")

    lines.extend(["## Discrepancies", ""])
    discrepancies = data.get("discrepancies") or []
    lines.extend(["| # | Discrepancy | Evidence / note |", "|---:|---|---|"])
    if discrepancies:
        for i, d in enumerate(discrepancies, start=1):
            if isinstance(d, dict):
                lines.append(f"| {i} | {_cell(d.get('issue') or d.get('claim') or d, max_len=420)} | {_cell(d.get('evidence') or d.get('notes') or d, max_len=420)} |")
            else:
                lines.append(f"| {i} | {_cell(d, max_len=420)} |  |")
    else:
        lines.append("| 1 | No discrepancy detected from supplied evidence. |  |")
    lines.append("")

    lines.extend(["## Gaps", ""])
    gaps = data.get("gaps") or []
    lines.extend(["| # | Gap / missing evidence |", "|---:|---|"])
    if gaps:
        for i, g in enumerate(gaps, start=1):
            lines.append(f"| {i} | {_cell(g, max_len=520)} |")
    else:
        lines.append("| 1 | No explicit gap reported by the evidence normalizer. |")
    lines.append("")

    noise = data.get("noise_exclusion_summary") or {}
    lines.extend(["## Noise exclusion decision table", ""])
    lines.extend(["| Category | Decision |", "|---|---|"])
    excluded = noise.get("excluded_categories") or []
    if excluded:
        for cat in excluded:
            lines.append(f"| {_cell(cat, max_len=160)} | Excluded from clean artifact |")
    else:
        lines.append("| Generic page noise | Excluded by product-only policy |")
    if noise.get("notes"):
        for note in noise.get("notes")[:6]:
            lines.append(f"| Note | {_cell(note, max_len=420)} |")
    lines.append("")

    q = data.get("quality") or {}
    lines.extend(["## Artifact quality summary", ""])
    lines.extend(["| Metric | Value |", "|---|---|"])
    for key in [
        "access_status", "product_page_confidence", "evidence_completeness",
        "has_text_evidence", "has_structured_evidence", "has_table_evidence",
        "has_visual_evidence", "has_upstream_indexed_evidence", "browser_visible",
        "product_details_recovered", "recovery_status", "created_by",
    ]:
        if key in q:
            lines.append(f"| {_cell(key, max_len=160)} | {_cell(q.get(key), max_len=420)} |")
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
    capture = page_capture_health(page)
    browser_visible = bool(capture.get("browser_product_visible"))
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
    recovery_sources.append("product_url_input")
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
        "capture_health": capture,
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
    source_alignment: SourceAlignmentContext,
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
        + len(evidence.source_specific_claims or [])
        + len(evidence.structured_claims or [])
        + len(evidence.table_claims or [])
        + len(evidence.visual_claims or [])
        + len(getattr(evidence, "upstream_indexed_claims", []) or [])
        + len(getattr(evidence, "url_derived_claims", []) or [])
        + len(getattr(evidence, "input_context_claims", []) or [])
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
        "browser_access_ok_or_recovered": bool(result.browser_visible or result.product_details_recovered or upstream_evidence.has_any() or input_context.has_any()),
        "source_alignment_documented": bool(evidence.source_alignment or source_alignment.model_report()),
        "fallback_claim_scope_safe": bool(source_alignment.requested_retailer_claims_allowed or source_alignment.source_specific_claim_scope == "scraped_source_only"),
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
    if not checks["source_alignment_documented"]:
        missing.append("source_alignment")

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
    capture = page_capture_health(page)
    if capture.get("weak_capture") and result.access_status == "accessible":
        warnings.append("HTTP 200 returned but product-page capture is weak; artifact relies on input/URL/context evidence where needed")
    if not getattr(result, "real_scrape_evidence", False):
        warnings.append("no strong Crawl4AI product-page capture selected; artifact may rely on input/URL evidence")
    if getattr(result, "capture_grade", "") in {"weak", "blocked_or_shell"}:
        warnings.append(f"selected Crawl4AI capture grade is {getattr(result, 'capture_grade', '')}")
    if source_alignment.alignment_status != "primary_requested_source":
        warnings.append(f"source alignment is {source_alignment.alignment_status}; source-specific commercial claims are scoped to {source_alignment.source_specific_claim_scope}")

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
    score += 5 if checks["source_alignment_documented"] else 0
    if claims_count >= 5:
        score += 5
    if len(warnings) >= 3:
        score -= 10
    if not getattr(result, "real_scrape_evidence", False):
        score -= 15
    if getattr(result, "capture_grade", "") == "blocked_or_shell":
        score -= 20
    elif getattr(result, "capture_grade", "") == "weak":
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
        recommended_followups.append("configure authorised target-country proxy if browser-rendered retailer evidence is mandatory")
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
            "source_specific_claims": len(evidence.source_specific_claims or []),
            "structured_claims": len(evidence.structured_claims or []),
            "table_claims": len(evidence.table_claims or []),
            "visual_claims": len(evidence.visual_claims or []),
            "url_derived_claims": len(getattr(evidence, "url_derived_claims", []) or []),
            "input_context_claims": len(getattr(evidence, "input_context_claims", []) or []),
            "product_only_text_blocks": len(evidence.product_only_text_blocks or []),
            "product_only_text_chars": product_text_chars,
            "downloaded_images": len(downloaded_images),
            "vision_described_images": len(described_images),
            "image_download_errors": len(download_errors),
            "image_http_403_errors": len(download_403),
            "tables": len(tables),
            "json_ld_blocks": len(page.json_ld or []),
        },
        "source_alignment": source_alignment.model_report(),
        "capture": {
            "capture_profile_used": getattr(result, "capture_profile_used", ""),
            "capture_profiles_attempted": getattr(result, "capture_profiles_attempted", []),
            "capture_score": getattr(result, "capture_score", 0),
            "capture_grade": getattr(result, "capture_grade", "not_evaluated"),
            "real_scrape_evidence": getattr(result, "real_scrape_evidence", False),
            "weak_capture_reasons": getattr(result, "weak_capture_reasons", []),
            "capture_health": capture,
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
    """Use LLM to turn normalized evidence JSON into the final business claims.md."""
    from .services.llm import get_llm_service

    user = (
        "Create claims.md from this normalized product-only evidence JSON. "
        "Do not add new facts. Do not include navigation/footer/recommendations/noise. "
        "The output must be business-readable and table-first. Use concise tables, not text-heavy prose. "
        "Include a Source Alignment table before claims. Keep product-level claims separate from source-specific commercial claims. "
        "Required sections: 1) Executive decision summary table, 2) Source alignment table, 3) Identity table, "
        "4) Product-level claims table, 5) Source-specific commercial claims table, 6) Visual evidence table, 7) Specifications/table evidence, "
        "6) Gaps and discrepancies table, 7) Final downstream-readiness decision. "
        "Every material row must include evidence axes and confidence.\n\n"
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
