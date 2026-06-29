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
from product_scraping_agent.batch_preflight import prepare_unique_batch_input_csv
from product_scraping_agent.business_validation import enrich_batch_output_csv
from product_scraping_agent.log import setup_logging
from product_scraping_agent.runtime_preflight import run_runtime_preflight, write_preflight_report
from product_scraping_agent.semantic_enrichment import enrich_artifact_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch scrape product URLs into one artifact folder per input row.")
    parser.add_argument("--input-csv", required=True, help="CSV containing product_url and optional context columns")
    parser.add_argument("--output-csv", required=True, help="Mapping CSV to write: input row → artifact paths/status")
    parser.add_argument("--summary-json", default="", help="Optional batch summary JSON path")
    parser.add_argument("--preflight-json", default="", help="Optional preflight report path for duplicate input_id handling")
    parser.add_argument("--runtime-preflight-json", default="", help="Optional runtime/environment preflight report path")
    parser.add_argument("--output-root", default="data/scraped", help="Root folder for per-product artifacts")
    parser.add_argument("--retailer-label", default="retailer", help="Artifact subfolder name under each scrape id")
    parser.add_argument("--max-concurrency", type=int, default=2, help="Number of URLs to scrape concurrently")
    parser.add_argument("--max-images", type=int, default=30)
    parser.add_argument("--vision-max", type=int, default=12)
    parser.add_argument("--max-agent-iterations", type=int, default=2)
    parser.add_argument("--resume", action="store_true", help="Append to output CSV and skip input_ids already marked success")
    parser.add_argument("--skip-existing-artifacts", action="store_true", help="Skip rows whose artifact_manifest.json already exists")
    parser.add_argument("--stop-on-error", action="store_true", help="Fail the batch on first row exception")
    parser.add_argument("--fail-on-duplicate-input-id", action="store_true", help="Fail before scraping if duplicate explicit input_id values are present")
    parser.add_argument("--skip-runtime-preflight", action="store_true", help="Skip import/config/output-root runtime preflight checks")
    parser.add_argument("--check-browser-launch", action="store_true", help="Launch Chromium during runtime preflight to verify browser installation")
    parser.add_argument("--skip-semantic-enrichment", action="store_true", help="Skip contract-safe artifact semantic enrichment after scraping")
    parser.add_argument("--write-raw-debug", action="store_true", help="Persist raw observed page markdown/html under debug_raw/")
    parser.add_argument("--disable-domain-profile-learning", action="store_true", help="Do not reorder Crawl4AI profiles based on earlier successful domains in this batch")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    setup_logging(args.log_level)
    if not args.skip_runtime_preflight:
        runtime_report = await run_runtime_preflight(
            output_root=Path(args.output_root),
            check_browser_launch=args.check_browser_launch,
        )
        write_preflight_report(runtime_report, Path(args.runtime_preflight_json) if args.runtime_preflight_json else None)
        if not runtime_report.ok:
            print("\nRUNTIME PREFLIGHT FAILED")
            print(json.dumps(runtime_report.as_dict(), ensure_ascii=False, indent=2))
            raise SystemExit(2)
    preflight = prepare_unique_batch_input_csv(
        Path(args.input_csv),
        output_csv=Path(args.output_csv),
        preflight_json=Path(args.preflight_json) if args.preflight_json else None,
        fail_on_duplicate_input_id=args.fail_on_duplicate_input_id,
    )
    if preflight.changed:
        print("\nBATCH PREFLIGHT")
        print(json.dumps(preflight.as_dict(), ensure_ascii=False, indent=2))
    summary = await run_batch(
        input_csv=preflight.effective_input_csv,
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
    if not args.skip_semantic_enrichment:
        enrichment_results = enrich_artifact_root(Path(args.output_root), retailer_label=args.retailer_label)
        summary.extra["semantic_enrichment"] = {
            "artifact_count": len(enrichment_results),
            "changed_artifacts": sum(1 for r in enrichment_results if r.changed_files),
            "warning_count": sum(len(r.warnings) for r in enrichment_results),
        }
    enrich_batch_output_csv(Path(args.output_csv))
    print("\nBATCH SUMMARY")
    print(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2))


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
