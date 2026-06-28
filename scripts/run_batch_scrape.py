"""Run batch product scraping from a CSV file.

Input CSV minimum columns:
    input_id,product_url

Recommended columns:
    input_id,product_url,main_text,ean,requested_retailer_name,requested_country_code,
    source_retailer_name,source_country_code,source_url_role
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from product_scraping_agent.batch import BatchOptions, run_batch
from product_scraping_agent.log import setup_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch scrape product URLs into one artifact folder per input row.")
    parser.add_argument("--input-csv", required=True, help="CSV containing product_url and optional context columns")
    parser.add_argument("--output-csv", required=True, help="Mapping CSV to write: input row → artifact paths/status")
    parser.add_argument("--summary-json", default="", help="Optional batch summary JSON path")
    parser.add_argument("--output-root", default="data/scraped", help="Root folder for per-product artifacts")
    parser.add_argument("--retailer-label", default="retailer", help="Artifact subfolder name under each scrape id")
    parser.add_argument("--max-concurrency", type=int, default=2, help="Number of URLs to scrape concurrently")
    parser.add_argument("--max-images", type=int, default=30)
    parser.add_argument("--vision-max", type=int, default=12)
    parser.add_argument("--max-agent-iterations", type=int, default=2)
    parser.add_argument("--resume", action="store_true", help="Append to output CSV and skip input_ids already marked success")
    parser.add_argument("--skip-existing-artifacts", action="store_true", help="Skip rows whose artifact_manifest.json already exists")
    parser.add_argument("--stop-on-error", action="store_true", help="Fail the batch on first row exception")
    parser.add_argument("--write-raw-debug", action="store_true", help="Persist raw observed page markdown/html under debug_raw/")
    parser.add_argument("--disable-domain-profile-learning", action="store_true", help="Do not reorder Crawl4AI profiles based on earlier successful domains in this batch")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    setup_logging(args.log_level)
    summary = await run_batch(
        input_csv=Path(args.input_csv),
        output_csv=Path(args.output_csv),
        summary_json=Path(args.summary_json) if args.summary_json else None,
        options=BatchOptions(
            output_root=Path(args.output_root),
            retailer_label=args.retailer_label,
            max_concurrency=args.max_concurrency,
            max_images=args.max_images,
            vision_max=args.vision_max,
            max_agent_iterations=args.max_agent_iterations,
            resume=args.resume,
            skip_existing_artifacts=args.skip_existing_artifacts,
            stop_on_error=args.stop_on_error,
            write_raw_debug=True if args.write_raw_debug else None,
            domain_profile_learning=not args.disable_domain_profile_learning,
        ),
    )
    print("\nBATCH SUMMARY")
    print(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2))


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
