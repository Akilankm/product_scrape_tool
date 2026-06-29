"""Run runtime/environment preflight checks for the product scraping agent."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from product_scraping_agent.runtime_preflight import run_runtime_preflight, write_preflight_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check runtime readiness before product scraping.")
    parser.add_argument("--output-root", default="data/scraped", help="Output root to test for writability")
    parser.add_argument("--report-json", default="data/runtime_preflight.json", help="Where to write the preflight JSON report")
    parser.add_argument("--check-browser-launch", action="store_true", help="Actually launch Playwright Chromium to verify browser install")
    parser.add_argument("--browser-timeout-seconds", type=int, default=25)
    parser.add_argument("--fail-on-warning", action="store_true", help="Exit non-zero when warnings are present")
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    report = await run_runtime_preflight(
        output_root=Path(args.output_root),
        check_browser_launch=args.check_browser_launch,
        browser_timeout_seconds=args.browser_timeout_seconds,
    )
    write_preflight_report(report, Path(args.report_json) if args.report_json else None)
    payload = report.as_dict()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if report.status == "failed":
        raise SystemExit(2)
    if args.fail_on_warning and payload.get("warning_checks"):
        raise SystemExit(3)


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
