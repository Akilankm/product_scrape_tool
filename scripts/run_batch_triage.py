"""Create a prioritized triage CSV from a batch output CSV."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from product_scraping_agent.batch_triage import triage_batch_output_csv


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a prioritized triage report from batch scrape output.")
    parser.add_argument("--input-csv", required=True, help="Batch output CSV from run_batch_scrape.py")
    parser.add_argument("--output-csv", default="data/batch_triage.csv", help="Prioritized triage CSV path")
    parser.add_argument("--summary-json", default="data/batch_triage_summary.json", help="Summary JSON path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = triage_batch_output_csv(
        Path(args.input_csv),
        output_csv=Path(args.output_csv),
        summary_json=Path(args.summary_json) if args.summary_json else None,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
