"""Audit completed product scraping artifacts.

Example:
    pdm run audit-artifacts --output-root data/scraped --output-csv data/artifact_audit.csv
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from product_scraping_agent.artifact_audit import audit_artifact_root, write_audit_outputs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit product scraping artifact folders for completeness and coding readiness.")
    parser.add_argument("--output-root", default="data/scraped", help="Root folder containing one subfolder per scrape id")
    parser.add_argument("--retailer-label", default="retailer", help="Artifact subfolder name under each scrape id")
    parser.add_argument("--output-csv", default="data/artifact_audit.csv", help="Audit CSV path")
    parser.add_argument("--summary-json", default="data/artifact_audit_summary.json", help="Audit summary JSON path")
    parser.add_argument("--fail-on-not-ready", action="store_true", help="Exit with status 2 when any artifact is not ready for coding")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rows = audit_artifact_root(Path(args.output_root), retailer_label=args.retailer_label)
    summary = write_audit_outputs(
        rows,
        output_csv=Path(args.output_csv),
        output_json=Path(args.summary_json) if args.summary_json else None,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.fail_on_not_ready and summary.get("not_ready", 0):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
