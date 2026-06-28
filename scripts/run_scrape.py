"""Run the isolated product scraping agent for one product URL."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from product_scraping_agent import ProductScrapingAgent, ScrapeRequest, EvidenceSourceItem
from product_scraping_agent.log import setup_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape one known product URL into an artifact folder.")
    parser.add_argument("--url", required=True, help="Product page URL to scrape")
    parser.add_argument("--scrape-id", default=None, help="Optional stable artifact id")
    parser.add_argument("--main-text", default="", help="Optional source product text / item description")
    parser.add_argument("--ean", default="", help="Optional EAN/GTIN")
    parser.add_argument("--retailer-name", default="", help="Optional retailer name")
    parser.add_argument("--country-code", default="", help="Optional ISO country code, e.g. CZ. Supporting context for routing/trace only.")
    parser.add_argument("--proxy-url", default="", help="Optional proxy endpoint override. Prefer env/secret injection for credentials.")
    parser.add_argument("--proxy-country-code", default="", help="Optional proxy target country override. Defaults to country_code, then URL country hint.")
    parser.add_argument("--disable-proxy-retry", action="store_true", help="Disable same-URL proxy retry even if a proxy endpoint is configured.")
    parser.add_argument("--product-hint", default="", help="Optional override for image/claims prompt context")
    parser.add_argument("--upstream-ai-evidence", default="", help="Optional already-produced AI/search evidence text; no search is performed")
    parser.add_argument("--upstream-ai-evidence-file", default="", help="Optional text/markdown file containing upstream AI/search evidence")
    parser.add_argument("--candidate-snippet", action="append", default=[], help="Optional candidate/search snippet. Can be repeated.")
    parser.add_argument("--search-evidence-json", default="", help="Optional JSON file with list/dict of upstream search evidence items")
    parser.add_argument("--upstream-evidence-notes", default="", help="Optional notes about upstream evidence provenance")
    parser.add_argument("--output-root", default="data/scraped", help="Artifact output root")
    parser.add_argument("--retailer-label", default="retailer", help="Folder name under scrape id")
    parser.add_argument("--max-images", type=int, default=30)
    parser.add_argument("--vision-max", type=int, default=12)
    parser.add_argument("--max-agent-iterations", type=int, default=2)
    parser.add_argument("--write-raw-debug", action="store_true", help="Persist raw observed page markdown/html under debug_raw/")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def _load_upstream_items(path: str) -> list[EvidenceSourceItem]:
    if not path:
        return []
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "items" in data:
        data = data["items"]
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("--search-evidence-json must contain a JSON object, list, or {'items': [...]} payload")
    return [EvidenceSourceItem.model_validate(item) if isinstance(item, dict) else EvidenceSourceItem(text=str(item)) for item in data]


def _load_ai_evidence(args: argparse.Namespace) -> str:
    parts: list[str] = []
    if args.upstream_ai_evidence:
        parts.append(args.upstream_ai_evidence)
    if args.upstream_ai_evidence_file:
        parts.append(Path(args.upstream_ai_evidence_file).read_text(encoding="utf-8"))
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


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
            upstream_ai_evidence=_load_ai_evidence(args),
            candidate_snippets=args.candidate_snippet or [],
            search_evidence=_load_upstream_items(args.search_evidence_json),
            upstream_evidence_notes=args.upstream_evidence_notes,
            output_root=Path(args.output_root),
            retailer_label=args.retailer_label,
            max_images=args.max_images,
            vision_max=args.vision_max,
            max_agent_iterations=args.max_agent_iterations,
            write_raw_debug=args.write_raw_debug,
            proxy_url=args.proxy_url,
            proxy_country_code=args.proxy_country_code,
            enable_proxy_retry=not args.disable_proxy_retry,
        )
    )
    print("\nSCRAPE RESULT")
    print(f"success: {result.success}")
    print(f"scrape_id: {result.scrape_id}")
    print(f"output_dir: {result.output_dir}")
    print(f"product_evidence_json: {result.product_evidence_json_path}")
    print(f"product_evidence_md: {result.product_evidence_md_path}")
    print(f"claims_md: {result.claims_md_path}")
    print(f"evidence_recovery_report: {result.evidence_recovery_report_json_path}")
    print(f"browser_visible: {result.browser_visible}")
    print(f"product_details_recovered: {result.product_details_recovered}")
    print(f"recovery_status: {result.recovery_status}")
    print(f"evidence_axes_used: {result.evidence_axes_used}")
    print(f"url_analysis: {result.url_analysis.model_dump() if result.url_analysis else {}}")
    print(f"proxy_plan: {result.proxy_plan}")
    print(f"agent_iterations: {result.agent_iterations}")
    print(f"error: {result.error}")


if __name__ == "__main__":
    asyncio.run(main())
