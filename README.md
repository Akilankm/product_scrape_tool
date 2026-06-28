# Product Scraping Agent

A clean, isolated **product URL → product-only retailer evidence artifact** agent.

This repo intentionally contains only product scraping runtime code. It does **not** contain URL search/discovery, product coding, reporting spreadsheets, Streamlit UI, Docker search infrastructure, or rulebook logic.

## Contract

```text
INPUT   : product_url
OPTIONAL: main_text, ean, retailer_name, country_code, product_hint
OUTPUT  : noise-free product evidence artifact folder
```

The optional fields are provenance and identity hints. They help image relevance filtering, same-page evidence planning, and product-only evidence normalization. They never trigger search.

## What changed in v3

The scraper is now an **agentic evidence builder**, not a one-pass page dump:

```text
1. Initial Crawl4AI render of the supplied product URL
2. LLM planner checks whether evidence is complete enough
3. If needed, planner triggers same-URL follow-up capture only:
   - full_page_scroll
   - expand_common_sections
   - extract_gallery_sources
   - retry_relaxed
4. Images are downloaded, deduplicated, relevance-gated, and vision-described
5. LLM normalizer creates product-only evidence JSON/Markdown
6. claims.md is generated only from normalized product evidence
```

No web search is performed. No external facts are used. No guesses are allowed.

## Install

```bash
pdm install
pdm run playwright install chromium
```

Copy `.env.example` to `.env` or export the required `PCA_*` environment variables.

For deterministic no-LLM fallback mode:

```bash
export PCA_LLM_ENABLED=false
export PCA_LLM_VISION_ENABLED=false
```

> No-LLM mode is a degraded fallback. The production artifact is designed for LLM-enabled product-only normalization.

## Run from Python

```python
from pathlib import Path
from product_scraping_agent import ProductScrapingAgent, ScrapeRequest

result = await ProductScrapingAgent().scrape(
    ScrapeRequest(
        product_url="https://retailer.example/product/123",
        main_text="LEGO DUPLO 10965 Bath Time Fun",
        ean="5702017153647",
        retailer_name="Example Retailer",
        country_code="CZ",
        output_root=Path("data/scraped"),
        max_agent_iterations=2,
    )
)

print(result.output_dir)
print(result.product_evidence_json_path)
print(result.product_evidence_md_path)
print(result.claims_md_path)
```

## Run from CLI

```bash
pdm run python scripts/run_scrape.py \
  --url "https://retailer.example/product/123" \
  --main-text "LEGO DUPLO 10965 Bath Time Fun" \
  --ean "5702017153647" \
  --retailer-name "Example Retailer" \
  --country-code "CZ" \
  --max-agent-iterations 2 \
  --output-root data/scraped
```

Optional raw debug files can be written explicitly:

```bash
pdm run python scripts/run_scrape.py --url "..." --write-raw-debug
```

Raw debug is disabled by default so noisy full-page dumps do not contaminate the main artifact.

## Artifact layout

```text
data/scraped/<scrape_id>/
├── request.json
├── scrape_result.json
└── retailer/
    ├── source.md                     # product-only source text blocks, not raw page dump
    ├── product_evidence.json          # main machine-readable artifact
    ├── product_evidence.md            # main human-readable artifact
    ├── claims.md                      # final grounded retailer claim dossier
    ├── vision.md                      # retained product image observations
    ├── metadata.json                  # structured page metadata and capture counts
    ├── noise_report.json              # confirms noisy page/site content was excluded
    ├── tables/
    │   ├── table_001.md
    │   └── ...
    ├── images/
    │   ├── 001_<sha8>.jpg
    │   └── ...
    ├── manifests/
    │   ├── agent_trace.json
    │   ├── image_manifest.json
    │   ├── table_manifest.json
    │   └── artifact_manifest.json
    └── debug_raw/                     # only when --write-raw-debug / PCA_WRITE_RAW_DEBUG=true
        ├── observed_page.md
        └── observed_page.html
```

## Main downstream handoff

Use these files first:

| File | Purpose |
|---|---|
| `product_evidence.json` | Primary structured, product-only evidence object. Best input for product coding. |
| `product_evidence.md` | Human-readable product-only evidence dossier. |
| `claims.md` | Final grounded claim narrative generated from normalized evidence only. |
| `source.md` | Product-only text blocks retained from retailer evidence. Not raw noisy page text. |
| `vision.md` | Product-relevant image observations. |
| `metadata.json` | Canonical URL, title, JSON-LD, OG/product metadata, capture counts. |
| `tables/` | HTML tables converted to Markdown for spec evidence. |
| `images/` | Downloaded product images retained by relevance filtering. |
| `manifests/agent_trace.json` | LLM planner decisions and same-page iterative scrape actions. |

## Evidence axes

```text
T = product text from rendered page
V = product image / packaging visual evidence
S = structured metadata / JSON-LD / meta tags
D = HTML tables
I = user-provided input context: main_text, EAN, retailer, country
```

`I` is provenance only. The normalizer is instructed not to treat input context as a retailer claim unless it is also supported by `T`, `V`, `S`, or `D`.

## Runtime settings

Important environment variables:

```text
PCA_AGENTIC_ENABLED=true
PCA_AGENTIC_MAX_ITERATIONS=2
PCA_STRICT_PRODUCT_ONLY=true
PCA_WRITE_RAW_DEBUG=false
PCA_LLM_ENABLED=true
PCA_LLM_VISION_ENABLED=true
PCA_SCAN_FULL_PAGE=false
```

## Package structure

```text
src/product_scraping_agent/
├── agent.py          # Public ProductScrapingAgent API
├── pipeline.py       # Agentic URL → product-only artifact orchestration
├── agentic.py        # LLM planning + product-only normalization
├── full_scraper.py   # Crawl4AI rendering + follow-up profiles + HTML extraction
├── images.py         # Image download, dedupe, relevance, vision notes
├── models.py         # ScrapeRequest, ScrapeResult, ProductEvidence, ImageRef, TableRef
├── config.py         # PCA_* runtime settings
├── prompts.py        # Planner, evidence, claims, and vision prompts
├── services/
│   ├── scraper.py    # Shared Crawl4AI Chromium runtime
│   ├── llm.py        # Azure OpenAI wrapper
│   └── http.py       # HTTP/image helper utilities
└── ...
```

## Clean-scope guarantee

Removed from this codebase:

```text
product_discovery/
product_coding/
report-building scripts
spreadsheet inputs/outputs
Streamlit UI
database/search services
SearXNG/SerpAPI code
rulebook/API coding logic
```
