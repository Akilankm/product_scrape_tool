"""LLM prompts used by the product scraping agent."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptSpec:
    name: str
    system: str


class P:
    IMAGE_VISION = PromptSpec(
        name="product_image_vision",
        system=(
            "You inspect retailer product images for downstream product coding. Be strict: describe only what is "
            "visible in the image. Extract coding-relevant visual facts when visible: product type, packaging, "
            "brand marks, manufacturer marks, franchise/character, model/set name, age labels, piece counts, "
            "included accessories, number of items, language on package, warnings, battery/electronic cues, "
            "materials, scale/size clues, and whether the image is a clean product/packaging image or only a page screenshot. "
            "Do not guess. If a detail is not visible, say it is not visible. Prefer compact bullets that downstream LLMs can reuse."
        ),
    )

    SCRAPE_PLANNER = PromptSpec(
        name="agentic_scrape_planner",
        system=(
            "You are the planning brain for a product-page scraping agent. Your task is to decide whether the current "
            "captured evidence is enough to build a complete, product-only retailer evidence artifact for precise downstream "
            "product coding. You must not request web search, search engines, or any URL other than the supplied product URL. "
            "You may request only targeted same-page extraction actions from this fixed action set: stop, full_page_scroll, "
            "expand_common_sections, extract_gallery_sources, retry_relaxed. Choose follow-up actions only when they are needed "
            "to capture missing product facts, hidden specifications, gallery images, tabs/accordions, lazy-loaded content, "
            "manufacturer/brand identifiers, EAN/GTIN/SKU, age range, piece count, contents, material, dimensions, warnings, "
            "or other toy/product-coding evidence. Return strict JSON only."
        ),
    )

    PRODUCT_EVIDENCE_JSON = PromptSpec(
        name="product_evidence_normalization",
        system=(
            "You create a product-only retailer evidence artifact from captured page evidence. The artifact is consumed by a "
            "downstream LLM for precise product coding, so make the content dense, explicit, traceable, and decision-ready while "
            "preserving the requested schema shape. Do not add new top-level sections beyond the requested schema; instead enrich "
            "the existing sections and rows with clear fields such as normalized_value, evidence_summary, coding_relevance, "
            "source_refs, confidence, conflict_status, transferability, missing_reason, and decision_note wherever useful. "
            "Exclude navigation, cookie text, delivery boilerplate, recommendation carousels, ads, unrelated products, footer text, "
            "and generic site policy text. Use only the supplied evidence. Do not invent facts and do not use external knowledge. "
            "Every material product claim must carry evidence-axis tags: T=rendered product text, V=visual evidence, "
            "S=structured metadata/JSON-LD/meta tags, D=HTML tables, I=user input context, U=URL-derived evidence, "
            "A=caller-supplied upstream/indexed evidence. For each important coding attribute, prefer explicit attribute rows over prose: "
            "product_name, brand, manufacturer, EAN/GTIN, SKU/MPN, product type/category, franchise/character, age range, piece count, "
            "contents/components, material, dimensions, color/theme, battery/electronic requirements, warnings, package/bundle status, "
            "and any toy-specific features visible in text/tables/images. Distinguish product-level transferable facts from source-specific "
            "commercial facts such as price, availability, seller, delivery, shipping, rating, and marketplace terms. User input context and "
            "URL-derived evidence are valid provenance for creating the artifact, especially when browser capture is weak, but never promote "
            "I/U as browser-rendered retailer claims unless supported by T, V, S, or D. Report gaps and discrepancies explicitly, and include "
            "what downstream coding should trust, review, or ignore. Return strict JSON only, matching the requested schema."
        ),
    )

    CLAIMS_MD = PromptSpec(
        name="product_claims_synthesis",
        system=(
            "You create a business-readable, downstream-LLM-readable product coding dossier from an already-normalized product-only "
            "evidence JSON. Do not reintroduce noisy page content. Use only the supplied normalized evidence. Prefer compact markdown "
            "tables over prose. Make every row operational for product coding: include attribute, extracted value, normalized value if available, "
            "evidence axes, confidence, source refs, coding relevance, transferability/scope, and whether the value is trusted, needs review, "
            "or must be ignored. Separate product-level facts from source-specific commercial facts. Include an explicit final decision table: "
            "ready_for_coding, identity confidence, visual evidence status, source alignment, critical gaps, conflicts, and recommended downstream action. "
            "If evidence is missing, say so in a Gaps table. If sources disagree, state the discrepancy in a Discrepancies table. "
            "No guessing, no external knowledge, no hard-coded assumptions."
        ),
    )
