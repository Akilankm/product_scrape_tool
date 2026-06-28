"""High-level product scraping agent API."""

from __future__ import annotations

from pathlib import Path

from .config import Config, get_config
from .models import ScrapeRequest, ScrapeResult, ScrapedProduct
from .pipeline import scrape_product, write_failed_artifact_for_request


class ProductScrapingAgent:
    """Thin orchestrator around Crawl4AI + image + claims artifact creation."""

    def __init__(self, config: Config | None = None, output_root: Path | None = None) -> None:
        self.config = config or get_config()
        self.output_root = output_root

    async def scrape(self, request: ScrapeRequest) -> ScrapeResult:
        output_root = request.output_root or self.output_root
        try:
            rich: ScrapedProduct = await scrape_product(
                request.product_url,
                scrape_id=request.scrape_id,
                config=self.config,
                output_root=output_root,
                retailer_label=request.retailer_label or "retailer",
                product_hint=request.resolved_product_hint(),
                main_text=request.main_text,
                ean=request.ean,
                retailer_name=request.retailer_name,
                country_code=request.country_code,
                requested_retailer_name=request.requested_retailer_name,
                requested_country_code=request.requested_country_code,
                source_retailer_name=request.source_retailer_name,
                source_country_code=request.source_country_code,
                source_url_role=request.source_url_role,
                source_alignment=request.source_alignment,
                upstream_evidence=request.upstream_evidence,
                max_images=request.max_images,
                vision_max=request.vision_max,
                max_agent_iterations=request.max_agent_iterations,
                strict_product_only=request.strict_product_only,
                write_raw_debug=request.write_raw_debug,
            )
        except Exception as exc:  # noqa: BLE001
            # Batch workers must finalize one artifact per row even on unexpected
            # exceptions; do not leave only request.json/manifests behind.
            rich = write_failed_artifact_for_request(
                request,
                error=f"{type(exc).__name__}: {exc}",
                config=self.config,
                output_root=output_root,
            )
        return rich.to_scrape_result()


__all__ = ["ProductScrapingAgent", "ScrapeRequest", "ScrapeResult"]
