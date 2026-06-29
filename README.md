# Product Scraping Agent

A clean, isolated **product URL → product-only retailer evidence artifact** agent.

This repository intentionally contains only product scraping runtime code. It does **not** contain URL search/discovery, product coding, reporting spreadsheets, Streamlit UI, Docker search infrastructure, or rulebook logic.

## Current release

```text
Version: 1.2.6
Backend: Crawl4AI only
Scope  : supplied product URL only; no search and no alternate URL discovery
```

### What is covered through v1.2.6

```text
v1.2.3  Crawl4AI multi-profile same-URL capture
v1.2.4  strict capture decisions and batch domain-profile learning
v1.2.5  worker-safe row finalization and mandatory image evidence
v1.2.6  Crawl4AI MarkdownGenerationResult compatibility fix
```

The scraper is **Crawl4AI-only**. It does not use Firecrawl or any paid scraping backend.

## Install modes

### Core scraper install — recommended first

Use this when you only want the product scraping agent runtime. This does **not** install notebook dependencies, so it avoids the `ipykernel -> IPython -> jedi` chain.

```bash
pdm install --prod
pdm run playwright install chromium
```

### Notebook install — optional

Install this only when you want to run notebooks.

```bash
pdm install -G notebook
```

### Test install — optional

```bash
pdm install -G test
pdm run pytest
```

If install hangs at `jedi`, it is almost always from the notebook dependency chain, not the core scraper. Use `pdm install --prod` for the scraper-only runtime.

## Runtime contract

```text
INPUT   : product_url
OPTIONAL: main_text, ean, requested retailer/country, actual source retailer/country, source_url_role, product_hint
OPTIONAL RECOVERY EVIDENCE: upstream_ai_evidence, candidate_snippets, search_evidence
OUTPUT  : product-only evidence artifact folder
```

The optional fields are provenance and identity hints. They help image relevance filtering, same-page evidence planning, and product-only evidence normalization. They never trigger search.

## Source alignment contract

The provided `product_url` is always treated as the **actual evidence source**.

The requested retailer/country and the actual scraped source may differ:

```text
requested_retailer_name = original target retailer
requested_country_code  = original target country
source_retailer_name    = retailer/domain represented by product_url
source_country_code     = country/market represented by product_url
source_url_role         = primary_requested_retailer / alternate_retailer_same_country / global_fallback / etc.
```

This prevents fallback-source evidence from being incorrectly treated as requested-retailer evidence.

## Same-URL Crawl4AI capture

The agent is an **agentic evidence builder**, not a one-pass page dump.

For each supplied URL, it can try multiple same-URL Crawl4AI profiles:

```text
standard
load_wait
full_page_scroll
expand_common_sections
extract_gallery_sources
shadow_iframe
retry_relaxed
```

Each profile is scored for real product evidence. The selected profile is written to artifacts and batch output using:

```text
capture_profile_used
capture_profiles_attempted
capture_score
capture_grade
capture_decision
real_scrape_evidence
weak_capture_reasons
```

This prevents a thin HTTP-200 shell page from being treated as a successful product scrape.

Configure the profile sequence in `.env`:

```env
PCA_SCRAPE_MULTI_PROFILE_ENABLED=true
PCA_SCRAPE_PROFILE_SEQUENCE=standard,load_wait,full_page_scroll,expand_common_sections,extract_gallery_sources,shadow_iframe,retry_relaxed
PCA_SCRAPE_PROFILE_EARLY_STOP_SCORE=82
PCA_SCRAPE_PROFILE_MAX_PROFILES=7
PCA_SCRAPE_ENABLE_STEALTH=true
```

## Visual evidence contract

For this project, product images are mandatory for automated product identification.

A clean product image is required for an artifact to be considered directly usable. If clean image recovery fails, the agent may keep lower-confidence visual rescue evidence, but the artifact must be marked for review.

Visual statuses:

| Status | Meaning |
|---|---|
| `final_product_images_available` | At least one clean product-gallery image was retained. |
| `unverified_images_retained` | Image files were retained, but not fully vision-confirmed as product images. |
| `screenshot_fallback_only` | Direct image recovery failed; page screenshot was retained as rescue visual evidence. Manual review required. |
| `image_recovery_failed` | Image candidates existed but no usable image file was retained. |
| `no_image_candidates` | No image candidates were discovered in the selected capture. |

Screenshot fallback is **not** treated as a clean product image. It is a rescue path for visual inspection and is always lower confidence than a retained product-gallery image.

Relevant settings:

```env
PCA_IMAGE_REQUIRED=true
PCA_IMAGE_KEEP_UNVERIFIED_ON_VISION_FAILURE=true
PCA_SCREENSHOT_FALLBACK_ENABLED=true
PCA_SCREENSHOT_TIMEOUT=25
PCA_SCREENSHOT_FULL_PAGE=false
```

## Worker-safe batch finalization

Batch mode is worker-based. Each input row is processed independently and should produce a finalized row artifact even if scraping, image recovery, or LLM normalization fails.

Every row should end with either:

```text
_COMPLETE.json
```

or:

```text
_FAILED.json
```

Even failed rows should write a minimal artifact containing:

```text
request.json
scrape_result.json
retailer/source.md
retailer/product_evidence.json
retailer/quality_report.json
retailer/source_alignment_report.json
retailer/vision.md
retailer/manifests/artifact_manifest.json
```

A row left with only `request.json` and `retailer/manifests/` is not a valid final state.

## Crawl4AI markdown compatibility

Crawl4AI may return markdown as a plain string in some versions and as a `MarkdownGenerationResult` object in others. v1.2.6 normalizes those payloads before logging, scoring, metadata writing, artifact writing, and failure finalization.

This prevents failures like:

```text
TypeError: object of type 'MarkdownGenerationResult' has no len()
```

## Run from Python

```python
from pathlib import Path
from product_scraping_agent import ProductScrapingAgent, ScrapeRequest

result = await ProductScrapingAgent().scrape(
    ScrapeRequest(
        product_url="https://retailer.example/product/123",
        main_text="LEGO DUPLO 10965 Bath Time Fun",
        ean="5702017153647",

        # Backward-compatible aliases for requested context
        retailer_name="Requested Retailer",
        country_code="CO",

        # Optional actual URL/source context when product_url is a fallback source
        source_retailer_name="Fallback Retailer",
        source_country_code="US",
        source_url_role="global_fallback",

        output_root=Path("data/scraped"),
        max_agent_iterations=2,

        # Optional: pass evidence already produced by search/discovery.
        # The scraper will not search; it only uses this as A-axis recovery evidence.
        upstream_ai_evidence="SerpAPI AI Mode / indexed evidence text here",
        candidate_snippets=["Retailer indexed snippet here"],
    )
)

print(result.output_dir)
print(result.product_evidence_json_path)
print(result.visual_evidence_status)
print(result.artifact_quality)
```

## Run from CLI

```bash
pdm run python scripts/run_scrape.py \
  --url "https://retailer.example/product/123" \
  --main-text "LEGO DUPLO 10965 Bath Time Fun" \
  --ean "5702017153647" \
  --requested-retailer-name "Requested Retailer" \
  --requested-country-code "CO" \
  --source-retailer-name "Fallback Retailer" \
  --source-country-code "US" \
  --source-url-role "global_fallback" \
  --max-agent-iterations 2 \
  --upstream-ai-evidence-file evidence/ai_mode.txt \
  --candidate-snippet "Indexed retailer snippet..." \
  --search-evidence-json evidence/search_evidence.json \
  --output-root data/scraped
```

## Run batch CSV

```bash
pdm run scrape-batch \
  --input-csv data/samples/batch_input_sample.csv \
  --output-csv data/batch_scrape_output.csv \
  --summary-json data/batch_scrape_summary.json \
  --output-root data/scraped \
  --max-concurrency 2 \
  --resume
```

Recommended input CSV:

```csv
input_id,product_url,main_text,ean,requested_retailer_name,requested_country_code,source_retailer_name,source_country_code,source_url_role
P001,https://fallback.example/product/123,Product title,1234567890123,Requested Retailer,CO,Fallback Retailer,US,global_fallback
```

Batch output maps each input row to its artifact directory and diagnostics:

```text
input_id
product_url
success
artifact_quality
requires_manual_review
capture_decision
real_scrape_evidence
visual_evidence_status
image_failure_reason
artifact_dir
product_evidence_json_path
vision_md_path
quality_report_path
source_alignment_report_path
error
```

See `docs/batch_scraping.md` for the complete batch contract.

## Expected artifact structure

```text
data/scraped/<scrape_id>/
├── request.json
├── scrape_result.json
├── _COMPLETE.json or _FAILED.json
└── retailer/
    ├── source.md
    ├── product_evidence.json
    ├── product_evidence.md
    ├── claims.md
    ├── vision.md
    ├── metadata.json
    ├── quality_report.json
    ├── source_alignment_report.json
    ├── evidence_recovery_report.json
    ├── noise_report.json
    ├── images/
    ├── tables/
    └── manifests/
        ├── agent_trace.json
        ├── artifact_manifest.json
        ├── image_manifest.json
        └── table_manifest.json
```

## Quality interpretation

| Signal | Meaning |
|---|---|
| `success=true` | The artifact was created, not necessarily that the page was fully scraped. |
| `real_scrape_evidence=true` | Crawl4AI captured meaningful product-page evidence beyond input/URL hints. |
| `visual_evidence_status=final_product_images_available` | Clean product visual evidence is present. |
| `requires_manual_review=true` | Do not send directly to automated product coding without inspection. |
| `artifact_quality=insufficient` | Artifact exists but is not reliable enough for downstream automated coding. |

## No-search boundary

The scraper never performs web search or URL discovery.

If upstream systems already produced indexed snippets or AI Mode evidence, they can be passed explicitly as recovery evidence. Those claims are tagged as `A` evidence axis and remain distinguishable from browser-rendered page evidence.
