# Product Scraping Agent

A clean, isolated **product URL → product-only retailer evidence artifact** agent.

This repo intentionally contains only product scraping runtime code. It does **not** contain URL search/discovery, product coding, reporting spreadsheets, Streamlit UI, Docker search infrastructure, or rulebook logic.


## Install modes

### Core scraper install — recommended first

Use this when you only want the product scraping agent runtime. This does **not** install notebook dependencies, so it avoids the `ipykernel -> IPython -> jedi` chain.

```bash
pdm install --prod
```

### Notebook install — optional

Install this only when you want to run `notebooks/run_single_url_scrape.ipynb`.

```bash
pdm install -G notebook
```

### Test install — optional

```bash
pdm install -G test
pdm run pytest
```

If install hangs at `jedi`, it is almost always from the notebook dependency chain, not the core scraper. Use `pdm install --prod` for the scraper-only runtime.

## Contract

```text
INPUT   : product_url
OPTIONAL: main_text, ean, requested retailer/country, actual source retailer/country, source_url_role, product_hint
OPTIONAL RECOVERY EVIDENCE: upstream_ai_evidence, candidate_snippets, search_evidence
OUTPUT  : noise-free product evidence artifact folder
```

The optional fields are provenance and identity hints. They help image relevance filtering, same-page evidence planning, and product-only evidence normalization. They never trigger search.

## What changed by v1.2.4

The scraper is now an **agentic evidence builder**, not a one-pass page dump:

```text
1. Multi-profile Crawl4AI capture of the supplied product URL only
   - standard
   - load_wait
   - full_page_scroll
   - expand_common_sections
   - extract_gallery_sources
   - shadow_iframe
   - retry_relaxed
2. Each profile is scored for real product evidence, not just HTTP 200
3. The richest valid same-URL capture is selected before LLM normalization
4. LLM planner can still trigger additional same-URL follow-up capture only:
   - full_page_scroll
   - expand_common_sections
   - extract_gallery_sources
   - retry_relaxed
5. Images are downloaded, deduplicated, relevance-gated, and vision-described
6. LLM normalizer creates product-only evidence JSON/Markdown
7. claims.md is generated only from normalized product evidence
8. If browser access is blocked/weak, Evidence Recovery Mode can use caller-supplied upstream AI/search evidence without performing search itself
9. Image CDN recovery retries with browser-like headers/referers and optional Playwright request fallback
10. Deterministic quality gate writes `quality_report.json` for downstream acceptance/manual-review decisions
11. Source alignment separates the requested retailer/country from the actual URL evidence source so fallback URLs do not leak retailer-specific claims
12. Capture scoring is stricter: empty 0KB captures get score 0, image candidates alone do not prove scraping, and `real_scrape_evidence=false` can no longer be labelled `strong`
13. Batch mode learns the best successful Crawl4AI profile per domain during the same run and tries that profile first for later URLs on that domain
```

No web search is performed inside this scraper. No external facts are used unless the caller supplies them as upstream evidence, and those claims are explicitly tagged with `A` evidence axis. No guesses are allowed.

## Crawl4AI multi-profile capture

The scraper is Crawl4AI-only. It does not use Firecrawl or any paid scraping backend.

Configure the same-URL profile sequence in `.env`:

```env
PCA_SCRAPE_MULTI_PROFILE_ENABLED=true
PCA_SCRAPE_PROFILE_SEQUENCE=standard,load_wait,full_page_scroll,expand_common_sections,extract_gallery_sources,shadow_iframe,retry_relaxed
PCA_SCRAPE_PROFILE_EARLY_STOP_SCORE=82
PCA_SCRAPE_PROFILE_MAX_PROFILES=7
PCA_SCRAPE_ENABLE_STEALTH=true
```

Every run records capture diagnostics in `metadata.json`, `quality_report.json`, `artifact_manifest.json`, `agent_trace.json`, `scrape_result.json`, and the batch output CSV:

```text
capture_profile_used
capture_profiles_attempted
capture_score
capture_grade
capture_decision
real_scrape_evidence
weak_capture_reasons
is_weak_capture
is_block_or_challenge
has_real_scrape_evidence
```

This prevents a thin HTTP-200 shell page from being treated as a successful product scrape.

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
print(result.product_evidence_md_path)
print(result.claims_md_path)
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
    ├── evidence_recovery_report.json  # browser/proxy/upstream evidence recovery audit
    ├── source_alignment_report.json   # requested context vs actual scraped source policy
    ├── quality_report.json            # deterministic artifact completeness gate
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
| `evidence_recovery_report.json` | Explains whether the browser saw the page, whether product details were recovered, and which evidence sources were used. |
| `source_alignment_report.json` | Separates requested retailer/country from actual scraped source and scopes fallback-source commercial claims safely. |
| `quality_report.json` | Deterministic quality gate: strong/usable/partial/insufficient, missing fields, warnings, and follow-up recommendations. |
| `tables/` | HTML tables converted to Markdown for spec evidence. |
| `images/` | Downloaded product images retained by relevance filtering. |
| `manifests/agent_trace.json` | LLM planner decisions and same-page iterative scrape actions. |


## Requested context vs actual evidence source

The provided `product_url` may be a fallback source. The agent does **not** assume that `retailer_name/country_code` equals the retailer/country of the URL.

Use these fields when the requested market and actual URL source differ:

| Field | Meaning |
|---|---|
| `requested_retailer_name` / `retailer_name` | Original target retailer from the business/search row. |
| `requested_country_code` / `country_code` | Original target country/market. |
| `source_retailer_name` | Actual retailer/source represented by `product_url`, if known. |
| `source_country_code` | Actual country/market of `product_url`, if known. |
| `source_url_role` | Role of the provided URL, e.g. `primary_requested_retailer`, `alternate_retailer_same_country`, `alternate_retailer_different_country`, `same_retailer_different_country`, `marketplace_fallback`, `global_fallback`, or `unknown`. |

Claim policy:

| Claim type | Fallback URL can support it? | Scope |
|---|---:|---|
| Brand, product name, EAN/GTIN, manufacturer, product features, contents, age range, images | Yes, if evidence-grounded | Product-level facts |
| Price, availability, delivery, seller, marketplace terms, shipping, ratings | Source-specific only unless alignment is primary | Do not transfer to requested retailer/country |

The report is written to `source_alignment_report.json` and reflected in `product_evidence.json`, `product_evidence.md`, `claims.md`, `quality_report.json`, and `artifact_manifest.json`.

## Evidence axes

```text
B = directly browser-rendered page
P = proxy/target-country rendered page
T = product text from rendered page
V = product image / packaging visual evidence
S = structured metadata / meta tags
J = JSON-LD product data
D = HTML tables
A = caller-supplied upstream indexed/search/AI evidence
U = URL-derived evidence from the supplied product URL
I = user-provided input context: main_text, EAN, retailer, country
```

`I` is provenance only. The normalizer is instructed not to treat input context as a retailer claim unless it is also supported by `B`, `P`, `T`, `V`, `S`, `J`, `D`, or `A`. `A` is used only when the caller passes upstream evidence; the scraper itself does not search.

## Runtime settings

Important environment variables:

```text
PCA_AGENTIC_ENABLED=true
PCA_AGENTIC_MAX_ITERATIONS=2
PCA_STRICT_PRODUCT_ONLY=true
PCA_WRITE_RAW_DEBUG=false
PCA_LLM_ENABLED=true
PCA_LLM_VISION_ENABLED=true
PCA_RELEVANCE_BATCH_ENABLED=true
PCA_IMAGE_RETRY_STRATEGIES_ENABLED=true
PCA_IMAGE_BROWSER_REQUEST_FALLBACK=true
PCA_SCAN_FULL_PAGE=false
PCA_EVIDENCE_RECOVERY_MODE=true
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

## Geo/access restrictions

Some retailer product URLs may exist but be inaccessible from the runtime geography, for example from India or from a locked-down Azure region. The scraper now treats this as an **access-status problem**, not as product absence.

The artifact records:

```text
access_status
access_issue_type
access_issue_reason
geo_restricted
proxy_used
proxy_source
access_attempts
```

If you have an authorised target-country proxy/VPN egress, configure it in `.env`:

```env
PCA_GEO_PROXY_ENABLED=true
PCA_GEO_RETRY_ON_ACCESS_BLOCK=true
PCA_PROXY_URL_CZ=http://user:pass@cz-proxy.example:8080
PCA_ACCEPT_LANGUAGE_CZ=cs-CZ,cs;q=0.9,en;q=0.7
```

Without a configured proxy, a geo/access-blocked page produces an artifact with `access_status` such as `geo_restricted`, `access_denied`, or `bot_challenge`. It will **not** claim that the product is missing. If upstream AI/search evidence is supplied by the caller, the agent can still build a recovered product evidence artifact and tag those claims as `A` axis.

## Evidence Recovery Mode

This mode addresses the case where the product page exists, but the runtime browser cannot see it because of geography, anti-bot, or retailer access policy. The scraper still does not search. It can consume evidence already produced upstream:

```python
ScrapeRequest(
    product_url="https://retailer.example/product/123",
    main_text="Toy name from input CSV",
    ean="1234567890123",
    country_code="CO",
    upstream_ai_evidence="AI Mode / indexed evidence already obtained by discovery",
    candidate_snippets=["Indexed snippet from search result"],
    search_evidence=[
        {
            "source_type": "serp",
            "title": "Retailer product result",
            "url": "https://retailer.example/product/123",
            "text": "Indexed snippet text"
        }
    ],
)
```

The artifact records:

```text
browser_visible
product_details_recovered
recovery_status
evidence_axes_used
evidence_recovery_report.json
```

## v1.1.6 clean image and Markdown contract

The final `retailer/images/` folder is now strict: it contains only final raster product images that passed rich vision validation with `RELATED: yes`.

Rejected candidates are not silently lost. They are audited in:

```text
retailer/manifests/image_manifest.json
```

This means `.bin`, `.html`, CDN error bodies, SVG chrome, unsupported payloads, and unverified/non-product candidates should not remain in the final `images/` folder.

Markdown handoff files are also more business-readable and table-first:

```text
source.md              # product-only extracted text table
vision.md              # retained image decision table + observations
product_evidence.md    # identity/claims/spec/visual/gap tables
claims.md              # business-level decision dossier
```


## v1.2.1 note — notebook + image count semantics

- The notebook is updated for fallback-source runs and includes weak-capture diagnostics.
- `image_candidate_count` means discovered image URLs/candidates.
- `image_count` and `final_image_count` mean final clean images retained under `retailer/images/` after vision confirmation.
- For weak Amazon-style captures, it is valid and expected for `image_candidate_count > 0` while `final_image_count == 0`. In that case, candidates are retained only in `manifests/image_manifest.json`, not as handoff images.

## v1.2.0 note — input/URL recovery for fallback links

The scraper now treats `product_url + main_text + ean + requested/source context` as sufficient to create the artifact. If the browser receives only a weak HTTP-200 shell page, the run is not stopped and the user is not forced to provide upstream evidence. The artifact is still created from:

- `I` = caller-provided input context (`main_text`, `ean`, requested retailer/country)
- `U` = URL-derived provenance/identity hints from the supplied `product_url`
- `T/S/D/V/A` = browser text, metadata, tables, vision/images, and optional upstream evidence when available

Weak capture is recorded in `metadata.json`, `evidence_recovery_report.json`, `quality_report.json`, and `artifact_manifest.json`. `result.error` is reserved for technical failures; quality/manual-review decisions are exposed through `result.artifact_quality`, `result.requires_manual_review`, and `quality_report.json`.

## Batch CSV scraping

Use batch mode when URL discovery has already produced a CSV of product URLs and you need one artifact folder per row plus a mapping output.

Minimum input:

```csv
input_id,product_url
P001,https://retailer.example/product/123
```

Recommended input:

```csv
input_id,product_url,main_text,ean,requested_retailer_name,requested_country_code,source_retailer_name,source_country_code,source_url_role
P001,https://fallback.example/product/123,Product title,1234567890123,Requested Retailer,CO,Fallback Retailer,US,global_fallback
```

Run:

```bash
pdm run python scripts/run_batch_scrape.py \
  --input-csv data/samples/batch_input_sample.csv \
  --output-csv data/batch_scrape_output.csv \
  --summary-json data/batch_scrape_summary.json \
  --output-root data/scraped \
  --max-concurrency 2 \
  --resume
```

Or:

```bash
pdm run scrape-batch \
  --input-csv data/samples/batch_input_sample.csv \
  --output-csv data/batch_scrape_output.csv \
  --output-root data/scraped
```

Batch output maps each row to its artifact:

```text
input_id → artifact_dir → product_evidence.json / claims.md / quality_report.json
```

Key output columns:

| Column | Meaning |
|---|---|
| `input_id` | Stable input row id. |
| `success` | Technical scrape/artifact generation status. |
| `artifact_quality` | Deterministic quality gate from `quality_report.json`. |
| `requires_manual_review` | Whether downstream users should review before coding. |
| `artifact_dir` | Root folder for the row's product evidence artifact. |
| `product_evidence_json_path` | Primary structured handoff artifact. |
| `claims_md_path` | Business-readable claim dossier. |
| `source_alignment_report_path` | Requested context vs actual scraped URL source. |
| `evidence_recovery_report_path` | Browser/URL/input/upstream evidence recovery audit. |

See `docs/batch_scraping.md` for the full contract.

## v1.2.5 visual-evidence and worker-finalization contract

Product images are mandatory for downstream product identification. From v1.2.5:

- every batch worker finalizes its row with `_COMPLETE.json` or `_FAILED.json`;
- `vision.md` is never empty;
- batch output includes `visual_evidence_status`, `image_required`, `screenshot_fallback_used`, and `image_failure_reason`;
- `artifact_quality=usable/strong` requires at least one clean downloaded product image;
- screenshot fallback is retained only as manual-review rescue evidence.

Expected visual statuses:

| Status | Meaning |
|---|---|
| `final_product_images_available` | Clean product image retained. |
| `unverified_images_retained` | Image retained but not cleanly vision-confirmed. |
| `screenshot_fallback_only` | Screenshot fallback retained; manual review required. |
| `image_recovery_failed` | No usable image retained. |
| `no_image_candidates` | No image candidates discovered. |


## v1.2.6 note — Crawl4AI markdown payload compatibility hardening

Crawl4AI may return `result.markdown` as a `MarkdownGenerationResult` object rather than a plain string. v1.2.6 defensively normalizes markdown/html payloads before logging, scoring, metadata creation, worker finalization, and artifact writing. This fixes row failures like:

```text
TypeError: object of type 'MarkdownGenerationResult' has no len()
```

The row should now continue to the normal artifact or worker-safe failure artifact instead of failing during diagnostics/finalization.
