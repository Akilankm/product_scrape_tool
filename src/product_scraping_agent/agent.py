"""High-level product scraping agent API."""

from __future__ import annotations

from pathlib import Path

from .config import Config, get_config
from .models import ScrapeRequest, ScrapeResult, ScrapedProduct
from .pipeline import scrape_product


class ProductScrapingAgent:
    """Thin orchestrator around Crawl4AI + image + claims artifact creation."""

    def __init__(self, config: Config | None = None, output_root: Path | None = None) -> None:
        self.config = config or get_config()
        self.output_root = output_root

    async def scrape(self, request: ScrapeRequest) -> ScrapeResult:
        output_root = request.output_root or self.output_root
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
            upstream_evidence=request.upstream_evidence,
            max_images=request.max_images,
            vision_max=request.vision_max,
            max_agent_iterations=request.max_agent_iterations,
            strict_product_only=request.strict_product_only,
            write_raw_debug=request.write_raw_debug,
        )
        return rich.to_scrape_result()


__all__ = ["ProductScrapingAgent", "ScrapeRequest", "ScrapeResult"]
