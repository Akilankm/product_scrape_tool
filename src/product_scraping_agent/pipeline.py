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
    build_noise_report,
    deterministic_product_evidence,
    normalize_product_evidence,
    plan_next_actions,
    render_product_evidence_md,
    synthesize_claims_md_from_evidence,
)
from .config import Config, get_config
from .full_scraper import FullPage, fetch_full, merge_full_pages, table_html_to_markdown
from .images import download_and_describe
from .log import logger
from .models import ImageRef, ProductEvidence, ProductInputContext, ScrapedProduct, TableRef
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


def _metadata_payload(page: FullPage, input_context: ProductInputContext, product_hint: str) -> dict[str, Any]:
    return {
        "input_context": input_context.model_dump(),
        "product_hint": product_hint,
        "requested_url": page.url,
        "final_url": page.final_url or page.url,
        "status": page.status,
        "success": page.success,
        "error": page.error,
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
    product_hint: str,
    out_dir: Path,
    result: ScrapedProduct,
    images: list[ImageRef],
    tables: list[TableRef],
    evidence: ProductEvidence | None,
) -> dict[str, Any]:
    quality = evidence.quality if evidence else {}
    return {
        "artifact_version": "product_scrape.v3.agentic_product_only",
        "scrape_id": scrape_id,
        "input_context": input_context.model_dump(),
        "product_hint": product_hint,
        "product_url": result.url,
        "final_url": result.final_url,
        "title": result.title,
        "product_only_artifact": True,
        "raw_debug_written": bool(result.raw_debug_dir),
        "files": {
            "source_md": _safe_rel(result.source_md_path, out_dir),
            "product_evidence_md": _safe_rel(result.product_evidence_md_path, out_dir),
            "product_evidence_json": _safe_rel(result.product_evidence_json_path, out_dir),
            "claims_md": _safe_rel(result.claims_md_path, out_dir),
            "vision_md": _safe_rel(result.vision_md_path, out_dir),
            "metadata_json": _safe_rel(result.metadata_json_path, out_dir),
            "noise_report_json": _safe_rel(result.noise_report_json_path, out_dir),
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
            "tables": len(tables),
            "json_ld_blocks": len(result.json_ld),
            "agent_iterations": result.agent_iterations,
            "retailer_claims": len(evidence.retailer_claims) if evidence else 0,
            "product_only_text_blocks": len(evidence.product_only_text_blocks) if evidence else 0,
        },
        "quality": {
            "page_captured": result.success,
            "input_context_provided": input_context.has_any(),
            "has_text_evidence": bool(result.raw_markdown),
            "has_structured_data": bool(result.json_ld),
            "has_visual_evidence": any(i.description for i in images),
            "has_table_evidence": bool(tables),
            "has_product_evidence_json": bool(evidence),
            "has_claims_synthesis": bool(result.claims_markdown),
            "product_page_confidence": quality.get("product_page_confidence", ""),
            "evidence_completeness": quality.get("evidence_completeness", ""),
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


async def _agentic_fetch_loop(
    cfg: Config,
    url: str,
    *,
    input_context: ProductInputContext,
    product_hint: str,
    max_iterations: int,
) -> tuple[FullPage, list[dict[str, Any]]]:
    """Initial scrape plus LLM-planned same-page follow-up passes."""
    trace: list[dict[str, Any]] = []
    page = await fetch_full(cfg, url, profile="standard")
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
            trace.append({
                "iteration": iteration,
                "plan": plan.model_dump(),
                "stopped": True,
                "stop_reason": plan.stop_reason or "planner reported enough evidence or no allowed action",
            })
            break

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
        followup = await fetch_full(cfg, url, profile=chosen.action)
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


def _source_md_from_product_evidence(evidence: ProductEvidence) -> str:
    """source.md is intentionally product-only, not a raw page dump."""
    data = evidence.model_dump()
    lines = ["# Product-only source evidence", ""]
    lines.append("This file contains product-relevant text blocks selected from the retailer page. It intentionally excludes navigation, footer, ads, recommendations, and generic site boilerplate.\n")
    blocks = data.get("product_only_text_blocks") or []
    if not blocks:
        lines.append("No normalized product-only text blocks were produced. See product_evidence.json for captured structured/table/visual claims.")
    for b in blocks:
        heading = b.get("heading") or "Product text"
        content = (b.get("content") or "").strip()
        axes = b.get("evidence_axis") or ["T"]
        lines.append(f"## {heading} ({','.join(axes)})")
        lines.append(content or "(empty)")
        lines.append("")
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
    input_context = ProductInputContext(
        main_text=main_text.strip(),
        ean=ean.strip(),
        retailer_name=retailer_name.strip(),
        country_code=country_code.strip(),
    )
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
    agent_trace_path = manifests_dir / "agent_trace.json"
    image_manifest_path = manifests_dir / "image_manifest.json"
    table_manifest_path = manifests_dir / "table_manifest.json"
    artifact_manifest_path = manifests_dir / "artifact_manifest.json"

    result = ScrapedProduct(
        scrape_id=sid,
        url=url,
        output_dir=out_dir,
        input_context=input_context,
        request_json_path=request_path,
        scrape_result_json_path=result_path,
        source_md_path=source_path,
        product_evidence_md_path=product_evidence_md_path,
        product_evidence_json_path=product_evidence_json_path,
        claims_md_path=claims_path,
        vision_md_path=vision_path,
        metadata_json_path=metadata_path,
        noise_report_json_path=noise_report_path,
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
        "product_hint": resolved_hint,
        "retailer_label": retailer_label,
        "output_root": str(out_root),
        "output_dir": str(out_dir),
        "max_images": max_images,
        "vision_max": vision_max,
        "agentic_enabled": cfg.agentic_enabled,
        "max_agent_iterations": loop_iterations,
        "strict_product_only": strict_product_only,
        "write_raw_debug": raw_debug_enabled,
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

    # Step 1: Fetch + agentic same-page loop.
    logger.info("")
    logger.info("── STEP 1/7: AGENTIC FETCH LOOP (same URL only) ──")
    t_step = time.monotonic()
    page, trace = await _agentic_fetch_loop(
        cfg,
        url,
        input_context=input_context,
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
    _write_json(agent_trace_path, trace)
    logger.info("  status    : {} success={} in {:.2f}s", page.status, page.success, time.monotonic() - t_step)
    logger.info("  final_url : {}", result.final_url)
    logger.info("  profiles  : {}", ", ".join(page.profiles_merged or [page.fetch_profile]))
    logger.info("  payload   : md={:,}B images={} tables={} json_ld={}",
                len(page.raw_markdown or ""), len(page.images), len(page.tables_html), len(page.json_ld))

    _write_json(metadata_path, _metadata_payload(page, input_context, resolved_hint))
    if raw_debug_enabled:
        result.raw_debug_dir = _write_debug_raw(out_dir, page)
        logger.info("  debug_raw : {}", result.raw_debug_dir)

    if not page.success or not (page.raw_markdown or page.raw_html):
        result.error = page.error or "empty page"
        result.elapsed_seconds = datetime.now(timezone.utc).timestamp() - t0
        _write_json(image_manifest_path, [])
        _write_json(table_manifest_path, [])
        _write_json(noise_report_path, {"raw_noise_text_persisted": False, "error": result.error})
        _write_json(artifact_manifest_path, _artifact_manifest(
            scrape_id=sid,
            input_context=input_context,
            product_hint=resolved_hint,
            out_dir=out_dir,
            result=result,
            images=[],
            tables=[],
            evidence=None,
        ))
        _write_json(result_path, result.to_scrape_result().model_dump())
        logger.error("  ✗ fetch failed: {}", result.error)
        return result

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
            page=page, tables=tables, images=refs, input_context=input_context,
            product_hint=resolved_hint, reason="PCA_LLM_ENABLED=false",
        )
    else:
        try:
            evidence = await asyncio.to_thread(
                normalize_product_evidence,
                page=page,
                tables=tables,
                images=refs,
                input_context=input_context,
                product_hint=resolved_hint,
                scrape_id=sid,
            )
        except Exception as exc:
            norm_err = f"{type(exc).__name__}: {exc}"
            logger.warning("  product evidence normalization failed: {}", norm_err)
            evidence = deterministic_product_evidence(
                page=page, tables=tables, images=refs, input_context=input_context,
                product_hint=resolved_hint, reason=norm_err,
            )
            result.error = f"product evidence fallback used: {norm_err}"

    # Stamp runtime quality values without overwriting LLM claims.
    evidence.quality.setdefault("agentic_iterations_used", result.agent_iterations)
    evidence.quality.setdefault("profiles_merged", page.profiles_merged)
    evidence.quality.setdefault("strict_product_only", strict_product_only)
    result.product_evidence = evidence.model_dump()

    evidence_md = render_product_evidence_md(evidence)
    source_md = _source_md_from_product_evidence(evidence)
    _write_json(product_evidence_json_path, evidence.model_dump())
    _write_text(product_evidence_md_path, evidence_md)
    _write_text(source_path, source_md)
    _write_json(noise_report_path, build_noise_report(evidence))
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

    # Step 7: Manifests.
    logger.info("")
    logger.info("── STEP 7/7: WRITE MANIFESTS ──")
    result.success = True
    result.elapsed_seconds = datetime.now(timezone.utc).timestamp() - t0
    _write_json(artifact_manifest_path, _artifact_manifest(
        scrape_id=sid,
        input_context=input_context,
        product_hint=resolved_hint,
        out_dir=out_dir,
        result=result,
        images=refs,
        tables=tables,
        evidence=evidence,
    ))
    _write_json(result_path, result.to_scrape_result().model_dump())
    logger.info("  ✓ artifact_manifest.json")
    logger.info("  ✓ scrape_result.json")
    logger.info("  complete  : scrape_id={} elapsed={:.1f}s", sid, result.elapsed_seconds)
    return result


__all__ = ["make_scrape_id", "scrape_product", "slug_from_url", "output_dir_for"]
