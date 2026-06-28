"""URL → clean product evidence artifact pipeline.

Boundary:
    Required input: product_url
    Optional input: main_text, ean, retailer_name, country_code, product_hint
    Output: noise-free product evidence artifact folder

There is no search, discovery, product coding, reporting, or UI logic here.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .agentic import (
    build_artifact_quality_report,
    build_evidence_recovery_report,
    build_noise_report,
    deterministic_product_evidence,
    evidence_axes_from_product_evidence,
    deterministic_product_details_recovered,
    normalize_product_evidence,
    plan_next_actions,
    render_product_evidence_md,
    synthesize_claims_md_from_evidence,
)
from .config import Config, get_config
from .full_scraper import FullPage, fetch_full, merge_full_pages, table_html_to_markdown
from .images import download_and_describe
from .log import logger
from .models import ImageRef, ProductEvidence, ProductInputContext, ScrapedProduct, SourceAlignmentContext, TableRef, UpstreamEvidenceBundle
from .text_utils import truncate_text

_MAX_TABLES = 40
_MAX_REPEAT_ACTIONS = 1
_ALLOWED_ACTIONS = {"full_page_scroll", "expand_common_sections", "extract_gallery_sources", "retry_relaxed"}


def make_scrape_id(prefix: str = "scrape") -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}_{uuid.uuid4().hex[:8]}"


def slug_from_url(url: str) -> str:
    parsed = urlparse(url)
    base = (parsed.path or "/").strip("/").replace("/", "_")
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-._") or "root"
    if len(base) > 80:
        base = base[:80]
    host = re.sub(r"[^A-Za-z0-9.-]+", "-", parsed.netloc).strip("-")
    return f"{host}__{base}" if host else base


def output_dir_for(
    scrape_id: str,
    url: str,
    *,
    output_root: Path,
    retailer_label: str = "retailer",
) -> Path:
    """Return `<output_root>/<scrape_id>/<retailer_label>/`."""
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", retailer_label or "retailer").strip("-._") or "retailer"
    out = output_root / scrape_id / label
    out.mkdir(parents=True, exist_ok=True)
    return out


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "model_dump"):
        return _to_jsonable(value.model_dump())
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(v) for v in value]
    return value


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_to_jsonable(data), ensure_ascii=False, indent=2), encoding="utf-8")


def _safe_rel(path: Path | None, base: Path) -> str:
    if path is None:
        return ""
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def _tables_from_page(page: FullPage) -> list[TableRef]:
    refs: list[TableRef] = []
    for i, html in enumerate(page.tables_html[:_MAX_TABLES], start=1):
        md, caption, rows, cols = table_html_to_markdown(html)
        if not md or rows < 1:
            continue
        refs.append(TableRef(index=i, caption=caption, rows=rows, cols=cols, markdown=md))
    return refs


def _write_table_artifacts(out_dir: Path, tables: list[TableRef]) -> list[TableRef]:
    table_dir = out_dir / "tables"
    table_dir.mkdir(parents=True, exist_ok=True)
    updated: list[TableRef] = []
    for table in tables:
        path = table_dir / f"table_{table.index:03d}.md"
        blocks: list[str] = []
        if table.caption:
            blocks.append(f"# {table.caption}\n")
        blocks.append(f"<!-- rows={table.rows} cols={table.cols} -->\n")
        blocks.append(table.markdown.rstrip() + "\n")
        _write_text(path, "\n".join(blocks))
        updated.append(table.model_copy(update={"local_path": path}))
    return updated


def _metadata_payload(
    page: FullPage,
    input_context: ProductInputContext,
    source_alignment: SourceAlignmentContext,
    product_hint: str,
    upstream_evidence: UpstreamEvidenceBundle,
) -> dict[str, Any]:
    return {
        "input_context": input_context.model_dump(),
        "source_alignment": source_alignment.model_report(),
        "upstream_evidence_present": upstream_evidence.has_any(),
        "upstream_evidence": upstream_evidence.compact(max_chars=12_000) if upstream_evidence.has_any() else {},
        "product_hint": product_hint,
        "requested_url": page.url,
        "final_url": page.final_url or page.url,
        "status": page.status,
        "success": page.success,
        "error": page.error,
        "access_status": page.access_status,
        "access_issue_type": page.access_issue_type,
        "access_issue_reason": page.access_issue_reason,
        "geo_restricted": page.geo_restricted,
        "proxy_used": page.proxy_used,
        "proxy_source": page.proxy_source,
        "access_attempts": page.access_attempts,
        "title": page.title,
        "description": page.description,
        "canonical_url": page.canonical_url,
        "og": page.og,
        "product_meta": page.product_meta,
        "json_ld": page.json_ld,
        "profiles_merged": page.profiles_merged,
        "counts": {
            "raw_html_chars": len(page.raw_html or ""),
            "raw_markdown_chars": len(page.raw_markdown or ""),
            "image_candidates": len(page.images),
            "tables_html": len(page.tables_html),
            "json_ld_blocks": len(page.json_ld or []),
        },
    }


def _image_manifest(images: list[ImageRef], out_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i, img in enumerate(images, start=1):
        rows.append({
            "index": i,
            "source_url": img.url,
            "local_path": _safe_rel(img.local_path, out_dir) if img.local_path else "",
            "width": img.width,
            "height": img.height,
            "bytes_size": img.bytes_size,
            "sha8": img.sha8,
            "phash": img.phash,
            "mime": img.mime,
            "alt": img.alt,
            "relevance": img.relevance,
            "description_available": bool(img.description),
            "description": img.description,
            "error": img.error,
            "download_source": img.download_source,
            "download_attempts": img.download_attempts,
        })
    return rows


def _table_manifest(tables: list[TableRef], out_dir: Path) -> list[dict[str, Any]]:
    return [
        {
            "index": t.index,
            "caption": t.caption,
            "rows": t.rows,
            "cols": t.cols,
            "local_path": _safe_rel(t.local_path, out_dir) if t.local_path else "",
            "markdown_chars": len(t.markdown or ""),
        }
        for t in tables
    ]


def _artifact_manifest(
    *,
    scrape_id: str,
    input_context: ProductInputContext,
    source_alignment: SourceAlignmentContext,
    product_hint: str,
    out_dir: Path,
    result: ScrapedProduct,
    images: list[ImageRef],
    tables: list[TableRef],
    evidence: ProductEvidence | None,
    quality_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    quality = evidence.quality if evidence else {}
    return {
        "artifact_version": "product_scrape.v7.source_alignment",
        "scrape_id": scrape_id,
        "input_context": input_context.model_dump(),
        "source_alignment": source_alignment.model_report(),
        "product_hint": product_hint,
        "product_url": result.url,
        "final_url": result.final_url,
        "title": result.title,
        "product_only_artifact": True,
        "access_status": result.access_status,
        "access_issue_type": result.access_issue_type,
        "geo_restricted": result.geo_restricted,
        "proxy_used": result.proxy_used,
        "proxy_source": result.proxy_source,
        "raw_debug_written": bool(result.raw_debug_dir),
        "browser_visible": result.browser_visible,
        "product_details_recovered": result.product_details_recovered,
        "recovery_status": result.recovery_status,
        "evidence_axes_used": result.evidence_axes_used,
        "upstream_evidence_present": result.upstream_evidence.has_any(),
        "files": {
            "source_md": _safe_rel(result.source_md_path, out_dir),
            "product_evidence_md": _safe_rel(result.product_evidence_md_path, out_dir),
            "product_evidence_json": _safe_rel(result.product_evidence_json_path, out_dir),
            "claims_md": _safe_rel(result.claims_md_path, out_dir),
            "vision_md": _safe_rel(result.vision_md_path, out_dir),
            "metadata_json": _safe_rel(result.metadata_json_path, out_dir),
            "noise_report_json": _safe_rel(result.noise_report_json_path, out_dir),
            "evidence_recovery_report_json": _safe_rel(result.evidence_recovery_report_json_path, out_dir),
            "quality_report_json": _safe_rel(result.quality_report_json_path, out_dir),
            "source_alignment_report_json": _safe_rel(result.source_alignment_report_json_path, out_dir),
            "agent_trace_json": _safe_rel(result.agent_trace_json_path, out_dir),
            "image_manifest_json": _safe_rel(result.image_manifest_path, out_dir),
            "table_manifest_json": _safe_rel(result.table_manifest_path, out_dir),
            "artifact_manifest_json": _safe_rel(result.artifact_manifest_path, out_dir),
            "tables_dir": "tables/" if tables else "",
            "images_dir": "images/" if any(i.local_path for i in images) else "",
            "raw_debug_dir": _safe_rel(result.raw_debug_dir, out_dir) if result.raw_debug_dir else "",
        },
        "counts": {
            "image_candidates": len(images),
            "images_downloaded": sum(1 for i in images if i.local_path),
            "images_described": sum(1 for i in images if i.description),
            "image_download_errors": sum(1 for i in images if i.error),
            "image_cdn_403_errors": sum(1 for i in images if "403" in (i.error or "")),
            "tables": len(tables),
            "json_ld_blocks": len(result.json_ld),
            "agent_iterations": result.agent_iterations,
            "retailer_claims": len(evidence.retailer_claims) if evidence else 0,
            "product_only_text_blocks": len(evidence.product_only_text_blocks) if evidence else 0,
        },
        "quality": {
            "artifact_created": result.success,
            "browser_page_captured": result.browser_visible,
            "product_details_recovered": result.product_details_recovered,
            "input_context_provided": input_context.has_any(),
            "has_text_evidence": bool(result.raw_markdown),
            "has_structured_data": bool(result.json_ld),
            "has_visual_evidence": any(i.description for i in images),
            "has_table_evidence": bool(tables),
            "has_product_evidence_json": bool(evidence),
            "has_claims_synthesis": bool(result.claims_markdown),
            "product_page_confidence": quality.get("product_page_confidence", ""),
            "evidence_completeness": quality.get("evidence_completeness", ""),
            "access_status": result.access_status,
            "geo_restricted": result.geo_restricted,
            "proxy_used": result.proxy_used,
            "recovery_status": result.recovery_status,
            "evidence_axes_used": result.evidence_axes_used,
            "quality_gate": (quality_report or {}).get("artifact_quality", "not_evaluated"),
            "requires_manual_review": bool((quality_report or {}).get("requires_manual_review", False)),
            "missing_critical_fields": (quality_report or {}).get("missing_critical_fields", []),
            "source_alignment_status": source_alignment.alignment_status,
            "requested_retailer_claims_allowed": source_alignment.requested_retailer_claims_allowed,
            "source_specific_claim_scope": source_alignment.source_specific_claim_scope,
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }




def _build_source_alignment_report(
    *,
    source_alignment: SourceAlignmentContext,
    final_url: str,
    page_title: str,
) -> dict[str, Any]:
    """Machine-readable policy for requested context vs scraped source.

    This is generic and does not hardcode any retailer. It prevents an alternate
    fallback URL from being treated as if it were the originally requested
    retailer/country.
    """
    report = source_alignment.model_report()
    report["scraped_source"]["final_url"] = final_url
    report["scraped_source"]["page_title"] = page_title
    report["claim_policy"] = {
        "product_level_claims": {
            "allowed": True,
            "scope": "product_identity_and_product_facts",
            "examples": ["brand", "product name", "EAN/GTIN", "manufacturer", "features", "contents", "age range", "images"],
            "condition": "must be evidence-grounded and tagged with source axes",
        },
        "retailer_specific_claims": {
            "allowed_for_requested_retailer": source_alignment.requested_retailer_claims_allowed,
            "scope": source_alignment.source_specific_claim_scope,
            "examples": ["price", "availability", "delivery", "seller", "marketplace terms", "ratings", "shipping"],
            "condition": "do not transfer from alternate/fallback source to requested retailer/country",
        },
    }
    report["interpretation"] = (
        "The provided product_url is the scraped evidence source. The requested retailer/country context "
        "may differ from that source. Downstream systems must use product-level facts only when evidence-grounded, "
        "and must keep source-specific commercial claims scoped to the scraped source unless alignment is primary."
    )
    return report


def _deterministic_followup_action(page: FullPage, used_actions: dict[str, int]) -> tuple[str, str] | None:
    """Safety-net planner when the LLM says stop too early.

    The LLM remains the brain, but the artifact contract needs deterministic
    guardrails: if the capture is clearly thin, do one same-URL expansion before
    downstream normalization. This never searches and never changes URL.
    """
    md_chars = len(page.raw_markdown or "")
    html_chars = len(page.raw_html or "")
    image_count = len(page.images or [])
    table_count = len(page.tables_html or [])
    jsonld_count = len(page.json_ld or [])

    def available(action: str) -> bool:
        return action in _ALLOWED_ACTIONS and used_actions.get(action, 0) < _MAX_REPEAT_ACTIONS

    if page.access_status != "accessible" and available("retry_relaxed"):
        return "retry_relaxed", "direct capture has access/visibility weakness; retry relaxed same-URL profile"
    if md_chars < 5_000 and html_chars > md_chars * 3 and available("expand_common_sections"):
        return "expand_common_sections", "rendered markdown is thin compared with HTML; expand common product sections"
    if md_chars < 8_000 and table_count == 0 and jsonld_count == 0 and available("full_page_scroll"):
        return "full_page_scroll", "low text plus no structured/table evidence; perform full-page scroll"
    if image_count < 3 and available("extract_gallery_sources"):
        return "extract_gallery_sources", "few image candidates detected; probe same-page gallery/source attributes"
    return None

async def _agentic_fetch_loop(
    cfg: Config,
    url: str,
    *,
    input_context: ProductInputContext,
    source_alignment: SourceAlignmentContext,
    product_hint: str,
    max_iterations: int,
) -> tuple[FullPage, list[dict[str, Any]]]:
    """Initial scrape plus LLM-planned same-page follow-up passes."""
    trace: list[dict[str, Any]] = []
    fetch_country = source_alignment.source_country_code or source_alignment.requested_country_code or input_context.country_code
    page = await fetch_full(cfg, url, profile="standard", country_code=fetch_country)
    trace.append({
        "iteration": 0,
        "profile": "standard",
        "reason": "initial capture",
        "counts": {
            "markdown_chars": len(page.raw_markdown or ""),
            "html_chars": len(page.raw_html or ""),
            "images": len(page.images),
            "tables": len(page.tables_html),
            "json_ld": len(page.json_ld),
        },
        "success": page.success,
        "status": page.status,
    })

    if not (cfg.agentic_enabled and cfg.llm_enabled) or max_iterations <= 0 or not page.success:
        return page, trace

    used_actions: dict[str, int] = {}
    for iteration in range(1, max_iterations + 1):
        try:
            plan = await asyncio.to_thread(plan_next_actions, page, input_context, product_hint)
        except Exception as exc:
            logger.warning("agent planner failed at iteration {}: {}", iteration, exc)
            trace.append({"iteration": iteration, "planner_error": f"{type(exc).__name__}: {exc}", "stopped": True})
            break

        action_items = [a for a in plan.actions if a.action in _ALLOWED_ACTIONS]
        if plan.enough_evidence or not action_items:
            deterministic = _deterministic_followup_action(page, used_actions)
            if deterministic is None:
                trace.append({
                    "iteration": iteration,
                    "plan": plan.model_dump(),
                    "stopped": True,
                    "stop_reason": plan.stop_reason or "planner reported enough evidence or no allowed action",
                })
                break
            action, reason = deterministic
            logger.info(
                "  agent guardrail : iteration={} action={} reason={}",
                iteration, action, reason[:180],
            )
            used_actions[action] = used_actions.get(action, 0) + 1
            followup = await fetch_full(cfg, url, profile=action, country_code=fetch_country)
            before = (len(page.raw_markdown or ""), len(page.images), len(page.tables_html), len(page.json_ld))
            page = merge_full_pages(page, followup)
            after = (len(page.raw_markdown or ""), len(page.images), len(page.tables_html), len(page.json_ld))
            trace.append({
                "iteration": iteration,
                "plan": plan.model_dump(),
                "deterministic_guardrail": {"action": action, "reason": reason},
                "followup_success": followup.success,
                "followup_status": followup.status,
                "before_counts": {"markdown_chars": before[0], "images": before[1], "tables": before[2], "json_ld": before[3]},
                "after_counts": {"markdown_chars": after[0], "images": after[1], "tables": after[2], "json_ld": after[3]},
            })
            continue

        action_items = sorted(action_items, key=lambda a: a.priority)
        chosen = None
        for item in action_items:
            if used_actions.get(item.action, 0) < _MAX_REPEAT_ACTIONS:
                chosen = item
                break
        if chosen is None:
            trace.append({
                "iteration": iteration,
                "plan": plan.model_dump(),
                "stopped": True,
                "stop_reason": "all proposed same-page actions already used",
            })
            break

        used_actions[chosen.action] = used_actions.get(chosen.action, 0) + 1
        logger.info("  agent plan : iteration={} action={} reason={}", iteration, chosen.action, chosen.reason[:180])
        followup = await fetch_full(cfg, url, profile=chosen.action, country_code=fetch_country)
        before = (len(page.raw_markdown or ""), len(page.images), len(page.tables_html), len(page.json_ld))
        page = merge_full_pages(page, followup)
        after = (len(page.raw_markdown or ""), len(page.images), len(page.tables_html), len(page.json_ld))
        trace.append({
            "iteration": iteration,
            "plan": plan.model_dump(),
            "executed_action": chosen.model_dump(),
            "followup_success": followup.success,
            "followup_status": followup.status,
            "before_counts": {"markdown_chars": before[0], "images": before[1], "tables": before[2], "json_ld": before[3]},
            "after_counts": {"markdown_chars": after[0], "images": after[1], "tables": after[2], "json_ld": after[3]},
        })
    return page, trace


def _write_debug_raw(out_dir: Path, page: FullPage) -> Path:
    debug_dir = out_dir / "debug_raw"
    debug_dir.mkdir(parents=True, exist_ok=True)
    _write_text(debug_dir / "observed_page.md", page.raw_markdown or "")
    _write_text(debug_dir / "observed_page.html", page.raw_html or "")
    return debug_dir


def _md_cell(value: Any, *, max_len: int = 700) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"\s+", " ", text).strip().replace("|", "\\|")
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def _source_md_from_product_evidence(evidence: ProductEvidence) -> str:
    """source.md is product-only and table-first, not a raw page dump."""
    data = evidence.model_dump()
    lines = [
        "# Product-only Source Evidence",
        "",
        "This file contains only product-relevant text blocks selected from the retailer page/evidence. Navigation, footer, ads, recommendations, cookie text, and generic boilerplate are intentionally excluded.",
        "",
        "| # | Section | Evidence axes | Product-only extracted text |",
        "|---:|---|---|---|",
    ]
    blocks = data.get("product_only_text_blocks") or []
    if not blocks:
        lines.append("| 1 | Not captured |  | No normalized product-only text block was produced. See `product_evidence.json` for structured/table/visual evidence. |")
    else:
        for i, b in enumerate(blocks, start=1):
            heading = b.get("heading") or "Product text"
            content = (b.get("content") or "").strip()
            axes = b.get("evidence_axis") or ["T"]
            lines.append(
                f"| {i} | {_md_cell(heading, max_len=180)} | {_md_cell(','.join(str(a) for a in axes), max_len=80)} | {_md_cell(content, max_len=900)} |"
            )
    lines.extend([
        "",
        "## Source policy",
        "",
        "| Rule | Decision |",
        "|---|---|",
        "| Raw page dump | Not emitted here |",
        "| Noisy site content | Excluded |",
        "| Product claims | Retained only when evidence-grounded |",
        "| Source alignment | Requested retailer/country and scraped source are tracked separately |",
        f"| Alignment status | {_md_cell((data.get('source_alignment') or {}).get('alignment_status', ''), max_len=180)} |",
        f"| Source-specific claim scope | {_md_cell((data.get('source_alignment') or {}).get('source_specific_claim_scope', ''), max_len=180)} |",
    ])
    return "\n".join(lines).strip() + "\n"


async def scrape_product(
    url: str,
    *,
    scrape_id: str | None = None,
    config: Config | None = None,
    output_root: Path | None = None,
    max_images: int = 30,
    vision_max: int = 12,
    retailer_label: str = "retailer",
    product_hint: str = "",
    main_text: str = "",
    ean: str = "",
    retailer_name: str = "",
    country_code: str = "",
    requested_retailer_name: str = "",
    requested_country_code: str = "",
    source_retailer_name: str = "",
    source_country_code: str = "",
    source_url_role: str = "unknown",
    source_alignment: SourceAlignmentContext | None = None,
    upstream_evidence: UpstreamEvidenceBundle | None = None,
    max_agent_iterations: int | None = None,
    strict_product_only: bool = True,
    write_raw_debug: bool | None = None,
) -> ScrapedProduct:
    """Scrape one known product URL into a noise-free product evidence artifact.

    The LLM acts as the planner and normalizer. It may request iterative,
    same-page extraction passes when evidence is incomplete. It may not perform
    search or invent facts.
    """
    cfg = config or get_config()
    sid = (scrape_id or make_scrape_id()).strip()
    requested_retailer = (requested_retailer_name or retailer_name or "").strip()
    requested_country = (requested_country_code or country_code or "").strip().upper()
    input_context = ProductInputContext(
        main_text=main_text.strip(),
        ean=ean.strip(),
        retailer_name=requested_retailer,
        country_code=requested_country,
    )
    source_alignment = source_alignment or SourceAlignmentContext(
        product_url=url,
        requested_retailer_name=requested_retailer,
        requested_country_code=requested_country,
        source_retailer_name=(source_retailer_name or "").strip(),
        source_country_code=(source_country_code or "").strip().upper(),
        source_url_role=(source_url_role or "unknown").strip(),
    )
    upstream_bundle = upstream_evidence or UpstreamEvidenceBundle()
    resolved_hint = (product_hint or "").strip() or input_context.compact_hint()
    out_root = output_root or cfg.output_root
    started = datetime.now(timezone.utc)
    t0 = started.timestamp()
    loop_iterations = cfg.agentic_max_iterations if max_agent_iterations is None else max(0, max_agent_iterations)
    raw_debug_enabled = cfg.write_raw_debug if write_raw_debug is None else bool(write_raw_debug)

    out_dir = output_dir_for(sid, url, output_root=out_root, retailer_label=retailer_label)
    scrape_root = out_dir.parent
    manifests_dir = out_dir / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)

    request_path = scrape_root / "request.json"
    result_path = scrape_root / "scrape_result.json"
    source_path = out_dir / "source.md"
    product_evidence_md_path = out_dir / "product_evidence.md"
    product_evidence_json_path = out_dir / "product_evidence.json"
    claims_path = out_dir / "claims.md"
    vision_path = out_dir / "vision.md"
    metadata_path = out_dir / "metadata.json"
    noise_report_path = out_dir / "noise_report.json"
    evidence_recovery_report_path = out_dir / "evidence_recovery_report.json"
    quality_report_path = out_dir / "quality_report.json"
    source_alignment_report_path = out_dir / "source_alignment_report.json"
    agent_trace_path = manifests_dir / "agent_trace.json"
    image_manifest_path = manifests_dir / "image_manifest.json"
    table_manifest_path = manifests_dir / "table_manifest.json"
    artifact_manifest_path = manifests_dir / "artifact_manifest.json"

    result = ScrapedProduct(
        scrape_id=sid,
        url=url,
        output_dir=out_dir,
        input_context=input_context,
        source_alignment=source_alignment,
        upstream_evidence=upstream_bundle,
        request_json_path=request_path,
        scrape_result_json_path=result_path,
        source_md_path=source_path,
        product_evidence_md_path=product_evidence_md_path,
        product_evidence_json_path=product_evidence_json_path,
        claims_md_path=claims_path,
        vision_md_path=vision_path,
        metadata_json_path=metadata_path,
        noise_report_json_path=noise_report_path,
        evidence_recovery_report_json_path=evidence_recovery_report_path,
        quality_report_json_path=quality_report_path,
        source_alignment_report_json_path=source_alignment_report_path,
        agent_trace_json_path=agent_trace_path,
        image_manifest_path=image_manifest_path,
        table_manifest_path=table_manifest_path,
        artifact_manifest_path=artifact_manifest_path,
    )

    _write_json(request_path, {
        "scrape_id": sid,
        "product_url": url,
        "main_text": input_context.main_text,
        "ean": input_context.ean,
        "retailer_name": input_context.retailer_name,
        "country_code": input_context.country_code,
        "requested_retailer_name": source_alignment.requested_retailer_name,
        "requested_country_code": source_alignment.requested_country_code,
        "source_retailer_name": source_alignment.source_retailer_name,
        "source_country_code": source_alignment.source_country_code,
        "source_url_role": source_alignment.source_url_role,
        "source_alignment": source_alignment.model_report(),
        "product_hint": resolved_hint,
        "upstream_evidence_present": upstream_bundle.has_any(),
        "upstream_evidence": upstream_bundle.compact(max_chars=20_000) if upstream_bundle.has_any() else {},
        "retailer_label": retailer_label,
        "output_root": str(out_root),
        "output_dir": str(out_dir),
        "max_images": max_images,
        "vision_max": vision_max,
        "agentic_enabled": cfg.agentic_enabled,
        "max_agent_iterations": loop_iterations,
        "strict_product_only": strict_product_only,
        "write_raw_debug": raw_debug_enabled,
        "geo_proxy_enabled": cfg.geo_proxy_enabled,
        "geo_retry_on_access_block": cfg.geo_retry_on_access_block,
        "created_at": started.isoformat(),
    })

    logger.info("")
    logger.info("╔══════════════════════════════════════════════════════════╗")
    logger.info("║  PRODUCT SCRAPING AGENT — agentic URL → artifact         ║")
    logger.info("╚══════════════════════════════════════════════════════════╝")
    logger.info("  scrape_id : {}", sid)
    logger.info("  url       : {}", url)
    logger.info("  out_dir   : {}", out_dir)
    logger.info("  mode      : product_only={} agentic={} max_iterations={}", strict_product_only, cfg.agentic_enabled, loop_iterations)
    if input_context.has_any():
        logger.info("  context   : main_text={!r} ean={} retailer={!r} country={}",
                    input_context.main_text[:70], input_context.ean or "-",
                    input_context.retailer_name or "-", input_context.country_code or "-")
    logger.info("  requested : retailer={!r} country={}", source_alignment.requested_retailer_name or "-", source_alignment.requested_country_code or "-")
    logger.info("  source    : retailer={!r} country={} role={} alignment={}", source_alignment.source_retailer_name or "-", source_alignment.source_country_code or "-", source_alignment.source_url_role, source_alignment.alignment_status)
    if upstream_bundle.has_any():
        logger.info("  upstream  : evidence bundle present (AI/search/snippets) — recovery mode enabled")

    # Step 1: Fetch + agentic same-page loop.
    logger.info("")
    logger.info("── STEP 1/7: AGENTIC FETCH LOOP (same URL only) ──")
    t_step = time.monotonic()
    page, trace = await _agentic_fetch_loop(
        cfg,
        url,
        input_context=input_context,
        source_alignment=source_alignment,
        product_hint=resolved_hint,
        max_iterations=loop_iterations,
    )
    result.agent_trace = trace
    result.agent_iterations = max(0, len([t for t in trace if t.get("executed_action")]))
    result.final_url = page.final_url or url
    result.title = page.title
    result.raw_markdown = page.raw_markdown
    result.raw_html = page.raw_html or ""
    result.json_ld = list(page.json_ld or [])
    result.access_status = page.access_status
    result.access_issue_type = page.access_issue_type
    result.access_issue_reason = page.access_issue_reason
    result.geo_restricted = page.geo_restricted
    result.proxy_used = page.proxy_used
    result.proxy_source = page.proxy_source
    result.access_attempts = list(page.access_attempts or [])
    result.browser_visible = bool(page.success and (page.raw_markdown or page.raw_html) and page.access_status == "accessible")
    source_alignment_report = _build_source_alignment_report(
        source_alignment=source_alignment,
        final_url=result.final_url,
        page_title=result.title,
    )
    _write_json(source_alignment_report_path, source_alignment_report)
    _write_json(agent_trace_path, trace)
    logger.info("  status    : {} success={} access={} proxy={} in {:.2f}s", page.status, page.success, page.access_status, page.proxy_source or "direct", time.monotonic() - t_step)
    logger.info("  final_url : {}", result.final_url)
    logger.info("  profiles  : {}", ", ".join(page.profiles_merged or [page.fetch_profile]))
    logger.info("  payload   : md={:,}B images={} tables={} json_ld={}",
                len(page.raw_markdown or ""), len(page.images), len(page.tables_html), len(page.json_ld))

    _write_json(metadata_path, _metadata_payload(page, input_context, source_alignment, resolved_hint, upstream_bundle))
    if raw_debug_enabled:
        result.raw_debug_dir = _write_debug_raw(out_dir, page)
        logger.info("  debug_raw : {}", result.raw_debug_dir)

    recoverable_evidence_present = bool(
        page.raw_markdown or page.raw_html or page.json_ld or page.og or page.product_meta
        or upstream_bundle.has_any()
        or input_context.has_any()
    )
    if (not page.success or not (page.raw_markdown or page.raw_html)) and not recoverable_evidence_present:
        result.error = page.error or page.access_issue_reason or "empty page and no recoverable evidence"
        result.elapsed_seconds = datetime.now(timezone.utc).timestamp() - t0
        _write_json(image_manifest_path, [])
        _write_json(table_manifest_path, [])
        empty_recovery = build_evidence_recovery_report(
            result=result,
            evidence=None,
            upstream_evidence=upstream_bundle,
            page=page,
        )
        _write_json(evidence_recovery_report_path, empty_recovery)
        _write_json(noise_report_path, {
            "raw_noise_text_persisted": False,
            "error": result.error,
            "access_status": result.access_status,
            "access_issue_type": result.access_issue_type,
            "access_issue_reason": result.access_issue_reason,
            "geo_restricted": result.geo_restricted,
            "proxy_used": result.proxy_used,
            "message": "Access failure does not imply the product is absent from the retailer site; no recoverable evidence was supplied or discovered.",
        })
        empty_quality_report = {
            "artifact_quality": "insufficient",
            "requires_manual_review": True,
            "missing_critical_fields": ["all_product_evidence"],
            "reason": result.error,
        }
        _write_json(quality_report_path, empty_quality_report)
        _write_json(artifact_manifest_path, _artifact_manifest(
            scrape_id=sid,
            input_context=input_context,
            source_alignment=source_alignment,
            product_hint=resolved_hint,
            out_dir=out_dir,
            result=result,
            images=[],
            tables=[],
            evidence=None,
            quality_report=empty_quality_report,
        ))
        _write_json(result_path, result.to_scrape_result().model_dump())
        logger.error("  ✗ no recoverable evidence: {}", result.error)
        return result
    if not page.success or not (page.raw_markdown or page.raw_html):
        logger.warning("  ! browser content weak/blocked; continuing with evidence recovery mode")

    # Step 2: Tables.
    logger.info("")
    logger.info("── STEP 2/7: TABLE EXTRACTION ──")
    tables = _write_table_artifacts(out_dir, _tables_from_page(page))
    result.tables = tables
    _write_json(table_manifest_path, _table_manifest(tables, out_dir))
    logger.info("  ✓ tables: {} persisted", len(tables))

    # Step 3: Images.
    logger.info("")
    logger.info("── STEP 3/7: IMAGES (download → dedup → relevance → vision) ──")
    refs: list[ImageRef] = []
    t_step = time.monotonic()
    if page.images:
        try:
            refs = await download_and_describe(
                page.images,
                referer=result.final_url,
                out_dir=out_dir,
                max_images=max_images,
                vision_max=vision_max,
                product_hint=resolved_hint,
                vision_concurrency=int(__import__("os").getenv("PCA_VISION_CONCURRENCY", "5")),
                download_concurrency=int(__import__("os").getenv("PCA_DOWNLOAD_CONCURRENCY", "8")),
            )
        except Exception as exc:
            logger.exception("  ✗ image stage failed: {}", exc)
    result.images = refs
    _write_json(image_manifest_path, _image_manifest(refs, out_dir))
    logger.info("  ✓ images: {} downloaded, {} vision-described in {:.2f}s",
                sum(1 for r in refs if r.local_path),
                sum(1 for r in refs if r.description),
                time.monotonic() - t_step)

    # Step 4: Product-only evidence normalization.
    logger.info("")
    logger.info("── STEP 4/7: PRODUCT-ONLY EVIDENCE NORMALIZATION ──")
    t_step = time.monotonic()
    evidence: ProductEvidence
    norm_err = ""
    if not cfg.llm_enabled:
        evidence = deterministic_product_evidence(
            page=page, tables=tables, images=refs, input_context=input_context, source_alignment=source_alignment,
            product_hint=resolved_hint, upstream_evidence=upstream_bundle, reason="PCA_LLM_ENABLED=false",
        )
    else:
        try:
            evidence = await asyncio.to_thread(
                normalize_product_evidence,
                page=page,
                tables=tables,
                images=refs,
                input_context=input_context,
                source_alignment=source_alignment,
                product_hint=resolved_hint,
                upstream_evidence=upstream_bundle,
                scrape_id=sid,
            )
        except Exception as exc:
            norm_err = f"{type(exc).__name__}: {exc}"
            logger.warning("  product evidence normalization failed: {}", norm_err)
            evidence = deterministic_product_evidence(
                page=page, tables=tables, images=refs, input_context=input_context, source_alignment=source_alignment,
                product_hint=resolved_hint, upstream_evidence=upstream_bundle, reason=norm_err,
            )
            result.error = f"product evidence fallback used: {norm_err}"

    # Stamp runtime/source-alignment values without overwriting LLM claims.
    if not evidence.source_alignment:
        evidence.source_alignment = source_alignment.model_report()
    evidence.quality.setdefault("source_alignment_status", source_alignment.alignment_status)
    evidence.quality.setdefault("requested_retailer_claims_allowed", source_alignment.requested_retailer_claims_allowed)
    evidence.quality.setdefault("source_specific_claim_scope", source_alignment.source_specific_claim_scope)
    evidence.quality.setdefault("agentic_iterations_used", result.agent_iterations)
    evidence.quality.setdefault("profiles_merged", page.profiles_merged)
    evidence.quality.setdefault("strict_product_only", strict_product_only)
    evidence.quality.setdefault("access_status", result.access_status)
    evidence.quality.setdefault("geo_restricted", result.geo_restricted)
    evidence.quality.setdefault("proxy_used", result.proxy_used)
    evidence.quality.setdefault("proxy_source", result.proxy_source)
    evidence.quality.setdefault("upstream_evidence_present", upstream_bundle.has_any())
    evidence.quality.setdefault("browser_visible", result.browser_visible)
    result.evidence_axes_used = evidence_axes_from_product_evidence(evidence)
    result.product_details_recovered = deterministic_product_details_recovered(evidence)
    recovery_report = build_evidence_recovery_report(
        result=result,
        evidence=evidence,
        upstream_evidence=upstream_bundle,
        page=page,
    )
    result.recovery_status = str(recovery_report.get("recovery_status") or ("recovered" if result.product_details_recovered else "insufficient_evidence"))
    evidence.quality.setdefault("product_details_recovered", result.product_details_recovered)
    evidence.quality.setdefault("recovery_status", result.recovery_status)
    evidence.quality.setdefault("evidence_axes_used", result.evidence_axes_used)
    result.product_evidence = evidence.model_dump()

    evidence_md = render_product_evidence_md(evidence)
    source_md = _source_md_from_product_evidence(evidence)
    _write_json(product_evidence_json_path, evidence.model_dump())
    _write_text(product_evidence_md_path, evidence_md)
    _write_text(source_path, source_md)
    _write_json(noise_report_path, build_noise_report(evidence))
    _write_json(evidence_recovery_report_path, recovery_report)
    logger.info("  ✓ product_evidence.json")
    logger.info("  ✓ product_evidence.md ({:,} chars)", len(evidence_md))
    logger.info("  ✓ source.md product-only ({:,} chars) in {:.2f}s", len(source_md), time.monotonic() - t_step)

    # Step 5: Vision markdown after evidence normalization so only retained images are described.
    logger.info("")
    logger.info("── STEP 5/7: VISION.MD ──")
    vision_lines = ["# Product image observations", "", "Only product-relevant images retained by the relevance gate are represented here.", ""]
    for i, img in enumerate(refs, start=1):
        if not img.local_path or not img.description:
            continue
        vision_lines.extend([
            f"## Image {i:03d} — `{img.local_path.name}`",
            f"- source_url: {img.url}",
            f"- alt: {img.alt or '(none)'}",
            f"- relevance: {img.relevance or 'unverified'}",
            "",
            img.description.strip(),
            "",
        ])
    if len(vision_lines) <= 4:
        vision_lines.append("No product image observations were available.")
    _write_text(vision_path, "\n".join(vision_lines).strip() + "\n")
    logger.info("  ✓ vision.md")

    # Step 6: Claims synthesis from normalized evidence only.
    logger.info("")
    logger.info("── STEP 6/7: CLAIMS.MD FROM PRODUCT-ONLY EVIDENCE ──")
    t_step = time.monotonic()
    md = ""
    last_err = ""
    if not cfg.llm_enabled:
        md = evidence_md
    else:
        for attempt in range(2):
            try:
                md = await asyncio.to_thread(synthesize_claims_md_from_evidence, evidence)
                if md:
                    break
                last_err = "empty completion"
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                logger.warning("  claims.md attempt {}/2 failed: {}", attempt + 1, last_err)
        if not md:
            md = evidence_md + f"\n\n> claims.md LLM rendering fallback used: {last_err or 'unknown error'}\n"
            if not result.error:
                result.error = f"claims.md LLM rendering fallback used: {last_err or 'unknown error'}"

    _write_text(claims_path, md)
    result.claims_markdown = md
    logger.info("  ✓ claims.md ({:,} chars) in {:.2f}s", len(md), time.monotonic() - t_step)

    # Step 6B: Deterministic artifact quality gate.
    quality_report = build_artifact_quality_report(
        evidence=evidence,
        result=result,
        page=page,
        tables=tables,
        images=refs,
        input_context=input_context,
        source_alignment=source_alignment,
        upstream_evidence=upstream_bundle,
    )
    _write_json(quality_report_path, quality_report)
    result.product_evidence.setdefault("quality_gate", quality_report)
    if quality_report.get("requires_manual_review") and not result.error:
        result.error = "artifact quality gate requires review: " + ", ".join(quality_report.get("missing_critical_fields", [])[:5])
    logger.info(
        "  ✓ quality_report.json quality={} review={} missing={}",
        quality_report.get("artifact_quality"),
        quality_report.get("requires_manual_review"),
        quality_report.get("missing_critical_fields", []),
    )

    # Step 7: Manifests.
    logger.info("")
    logger.info("── STEP 7/7: WRITE MANIFESTS ──")
    result.success = True
    result.elapsed_seconds = datetime.now(timezone.utc).timestamp() - t0
    _write_json(artifact_manifest_path, _artifact_manifest(
        scrape_id=sid,
        input_context=input_context,
        source_alignment=source_alignment,
        product_hint=resolved_hint,
        out_dir=out_dir,
        result=result,
        images=refs,
        tables=tables,
        evidence=evidence,
        quality_report=quality_report,
    ))
    _write_json(result_path, result.to_scrape_result().model_dump())
    logger.info("  ✓ artifact_manifest.json")
    logger.info("  ✓ scrape_result.json")
    logger.info("  complete  : scrape_id={} elapsed={:.1f}s", sid, result.elapsed_seconds)
    return result


__all__ = ["make_scrape_id", "scrape_product", "slug_from_url", "output_dir_for"]
