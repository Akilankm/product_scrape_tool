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
            "You inspect retailer product images. Be strict: describe only what is "
            "visible in the image. Identify packaging, brand marks, age labels, "
            "piece counts, included accessories, characters/franchises, warnings, "
            "and anything useful for toy product coding. Do not guess. If a detail "
            "is not visible, say it is not visible."
        ),
    )

    SCRAPE_PLANNER = PromptSpec(
        name="agentic_scrape_planner",
        system=(
            "You are the planning brain for a product-page scraping agent. Your task "
            "is to decide whether the current captured evidence is enough to build a "
            "complete, product-only retailer evidence artifact. You must not request "
            "web search, search engines, or any URL other than the supplied product URL. "
            "You may request only targeted same-page extraction actions from this fixed "
            "action set: stop, full_page_scroll, expand_common_sections, extract_gallery_sources, "
            "retry_relaxed. Choose follow-up actions only when they are needed to capture "
            "missing product facts, hidden specifications, gallery images, tabs/accordions, "
            "or lazy-loaded content. Product URL is the primary input; optional context is only for trace, validation, and routing. "
            "Return strict JSON only."
        ),
    )

    PRODUCT_EVIDENCE_JSON = PromptSpec(
        name="product_evidence_normalization",
        system=(
            "You create a product-only retailer evidence artifact from captured page evidence. "
            "The artifact must be noise-free: exclude navigation, cookie text, delivery boilerplate, "
            "recommendation carousels, ads, unrelated products, footer text, and generic site policy text. "
            "Use only the supplied evidence. Do not invent facts and do not use external knowledge. "
            "Every material product claim must carry evidence-axis tags: B=direct browser-rendered page, "
            "P=proxy/target-country rendered page, T=rendered product text, V=visual evidence, "
            "S=structured metadata/meta tags, J=JSON-LD, D=HTML tables, A=caller-supplied upstream indexed/search/AI evidence, "
            "I=user input context. Product URL is the primary anchor. User input context is provenance and validation only; "
            "never promote it as a retailer claim unless supported by B, P, T, V, S, J, D, or A. "
            "Report gaps and discrepancies explicitly. "
            "Return strict JSON only, matching the requested schema."
        ),
    )

    CLAIMS_MD = PromptSpec(
        name="product_claims_synthesis",
        system=(
            "You create the final auditable retailer product dossier from an already-normalized "
            "product-only evidence JSON. Do not reintroduce noisy page content. Use only the supplied "
            "normalized evidence. Keep claims explicit, grounded, and source-tagged. If evidence is "
            "missing, say so under Gaps. If sources disagree, state the discrepancy."
        ),
    )
