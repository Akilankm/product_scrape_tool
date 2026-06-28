"""Run the isolated product scraping agent for one product URL."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from product_scraping_agent import ProductScrapingAgent, ScrapeRequest
from product_scraping_agent.log import setup_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape one known product URL into an artifact folder.")
    parser.add_argument("--url", required=True, help="Product page URL to scrape")
    parser.add_argument("--scrape-id", default=None, help="Optional stable artifact id")
    parser.add_argument("--main-text", default="", help="Optional source product text / item description")
    parser.add_argument("--ean", default="", help="Optional EAN/GTIN")
    parser.add_argument("--retailer-name", default="", help="Optional retailer name")
    parser.add_argument("--country-code", default="", help="Optional ISO country code, e.g. CZ")
    parser.add_argument("--product-hint", default="", help="Optional override for image/claims prompt context")
    parser.add_argument("--output-root", default="data/scraped", help="Artifact output root")
    parser.add_argument("--retailer-label", default="retailer", help="Folder name under scrape id")
    parser.add_argument("--max-images", type=int, default=30)
    parser.add_argument("--vision-max", type=int, default=12)
    parser.add_argument("--max-agent-iterations", type=int, default=2)
    parser.add_argument("--write-raw-debug", action="store_true", help="Persist raw observed page markdown/html under debug_raw/")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    setup_logging(args.log_level)
    result = await ProductScrapingAgent().scrape(
        ScrapeRequest(
            product_url=args.url,
            scrape_id=args.scrape_id,
            main_text=args.main_text,
            ean=args.ean,
            retailer_name=args.retailer_name,
            country_code=args.country_code,
            product_hint=args.product_hint,
            output_root=Path(args.output_root),
            retailer_label=args.retailer_label,
            max_images=args.max_images,
            vision_max=args.vision_max,
            max_agent_iterations=args.max_agent_iterations,
            write_raw_debug=args.write_raw_debug,
        )
    )
    print("\nSCRAPE RESULT")
    print(f"success: {result.success}")
    print(f"scrape_id: {result.scrape_id}")
    print(f"output_dir: {result.output_dir}")
    print(f"product_evidence_json: {result.product_evidence_json_path}")
    print(f"product_evidence_md: {result.product_evidence_md_path}")
    print(f"claims_md: {result.claims_md_path}")
    print(f"agent_iterations: {result.agent_iterations}")
    print(f"error: {result.error}")


if __name__ == "__main__":
    asyncio.run(main())
