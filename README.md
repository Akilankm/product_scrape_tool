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
OPTIONAL: main_text, ean, retailer_name, country_code, product_hint
OPTIONAL ROUTING: proxy_url, proxy_country_code, enable_proxy_retry
OPTIONAL RECOVERY EVIDENCE: upstream_ai_evidence, candidate_snippets, search_evidence
OUTPUT  : noise-free product evidence artifact folder
```

The URL is the primary anchor. Optional fields are supporting context for decision trace, validation, image relevance, locale/proxy routing, and evidence normalization. They never trigger search and are not treated as retailer truth unless supported by captured evidence.

## What changed in v1.1.5

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
7. If browser access is blocked/weak, Evidence Recovery Mode can use caller-supplied upstream AI/search evidence without performing search itself
8. The artifact records `url_analysis`, `supporting_context_assessment`, `proxy_plan`, and planner decisions in the trace
```

No web search is performed inside this scraper. No external facts are used unless the caller supplies them as upstream evidence, and those claims are explicitly tagged with `A` evidence axis. No guesses are allowed.

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
        country_code="CZ",          # supporting routing/trace context
        output_root=Path("data/scraped"),
        max_agent_iterations=2,

        # Optional: pass evidence already produced by search/discovery.
        # The scraper will not search; it only uses this as A-axis recovery evidence.
        upstream_ai_evidence="SerpAPI AI Mode / indexed evidence text here",
        candidate_snippets=["Retailer indexed snippet here"],

        # Optional proxy controls. Proxy routing is native; endpoint credentials stay external.
        # proxy_url="http://user:pass@cz-proxy.example:8080",
        # proxy_country_code="CZ",
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
  --proxy-country-code "CZ" \
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
| `metadata.json` | URL analysis, proxy plan, canonical URL, title, JSON-LD, OG/product metadata, capture counts. |
| `evidence_recovery_report.json` | Explains whether the browser saw the page, whether product details were recovered, and which evidence sources were used. |
| `tables/` | HTML tables converted to Markdown for spec evidence. |
| `images/` | Downloaded product images retained by relevance filtering. |
| `manifests/agent_trace.json` | URL-first analysis, supporting-context assessment, LLM planner decisions, and same-page iterative scrape actions. |

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
PCA_SCAN_FULL_PAGE=false
PCA_EVIDENCE_RECOVERY_MODE=true
PCA_GEO_RETRY_ON_ACCESS_BLOCK=true
PCA_PROXY_URL_CZ=
PCA_ACCEPT_LANGUAGE_CZ=
```

## Package structure

```text
src/product_scraping_agent/
├── agent.py          # Public ProductScrapingAgent API
├── pipeline.py       # Agentic URL → product-only artifact orchestration
├── agentic.py        # LLM planning + product-only normalization
├── url_analysis.py   # URL decomposition and supporting-context assessment
├── proxy_router.py   # Native same-URL proxy/locale routing plan
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

Proxy retry orchestration is built in. If you have an authorised target-country proxy/VPN egress, configure only the endpoint/credentials in `.env`, YAML, request override, or a secret-injected environment variable:

```env
PCA_GEO_RETRY_ON_ACCESS_BLOCK=true
PCA_PROXY_URL_CZ=http://user:pass@cz-proxy.example:8080
PCA_ACCEPT_LANGUAGE_CZ=cs-CZ,cs;q=0.9,en;q=0.7
```

The agent resolves the proxy target from this priority order:

```text
1. request.proxy_country_code
2. request.country_code
3. URL country hint from the domain
```

Without a configured proxy endpoint, a geo/access-blocked page produces an artifact with `access_status` such as `geo_restricted`, `access_denied`, or `bot_challenge`. It will **not** claim that the product is missing. If upstream AI/search evidence is supplied by the caller, the agent can still build a recovered product evidence artifact and tag those claims as `A` axis.

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
