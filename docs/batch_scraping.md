# Batch Product Scraping

The batch runner turns a CSV of already-selected product URLs into one clean product evidence artifact per row.

The scraper does **not** perform search or URL discovery. Search/discovery must produce the product URL first; this batch runner only scrapes each supplied URL and writes a row-to-artifact mapping.

## Related docs

| Document | Use |
|---|---|
| [Usage guide](usage.md) | Commands and operating loop |
| [Artifact contract](artifact_contract.md) | Complete output schema |
| [Architecture](architecture.md) | Capture sequence and diagrams |
| [Runbook](runbook.md) | Review/retry/rescrape guidance |

## Minimum input CSV

```csv
input_id,product_url
P001,https://retailer.example/product/123
```

## Recommended input CSV

```csv
input_id,product_url,main_text,ean,requested_retailer_name,requested_country_code,source_retailer_name,source_country_code,source_url_role
P001,https://retailer.example/product/123,Product title,1234567890123,Requested Retailer,CZ,Actual URL Retailer,CZ,primary_requested_retailer
```

## Column meaning

| Column | Required | Meaning |
|---|---:|---|
| `input_id` | Recommended | Stable row/product id; used as artifact folder id |
| `product_url` | Yes | Exact URL to scrape |
| `main_text` | No | Product title/text from input or discovery stage |
| `ean` | No | EAN/GTIN when available |
| `requested_retailer_name` | No | Original business/target retailer |
| `requested_country_code` | No | Original target country code |
| `source_retailer_name` | No | Actual retailer/source represented by `product_url` |
| `source_country_code` | No | Country/market of `product_url` |
| `source_url_role` | No | Source role such as `primary_requested_retailer`, `global_fallback`, etc. |

Backward-compatible aliases are accepted: `url`, `retailer_name`, `country_code`, `EAN`, `MAIN_TEXT`, `RETAILER`, `COUNTRY`.

## Run batch

```bash
pdm run scrape-batch \
  --input-csv data/input.csv \
  --output-csv data/output.csv \
  --summary-json data/summary.json \
  --preflight-json data/preflight.json \
  --runtime-preflight-json data/runtime_preflight.json \
  --output-root data/scraped \
  --max-concurrency 2 \
  --resume
```

## Worker-safe output behavior

Batch mode processes rows in parallel up to `--max-concurrency`. Each worker finalizes its own row artifact independently.

A valid final row should have either:

```text
_COMPLETE.json
```

or:

```text
_FAILED.json
```

A row folder with only `request.json` and `retailer/manifests/` is not a valid final state.

## Output CSV highlights

| Field | Meaning |
|---|---|
| `artifact_dir` | Folder containing the product evidence artifact |
| `success` | Artifact was created; this does not automatically mean coding-ready |
| `artifact_quality` | Deterministic quality gate: `strong`, `usable`, `partial`, `insufficient`, etc. |
| `requires_manual_review` | Whether the artifact should be reviewed before downstream coding |
| `product_evidence_json_path` | Main machine-readable product evidence artifact |
| `claims_md_path` | Business/LLM-readable claim summary |
| `vision_md_path` | Visual evidence summary |
| `quality_report_path` | Deterministic quality gate details |
| `source_alignment_report_path` | Requested context vs actual scraped source alignment |
| `evidence_recovery_report_path` | Browser, URL/input, or upstream evidence recovery status |
| `error` | Technical exception if row failed unexpectedly |

## Artifact folder per row

```text
data/scraped/P001/
├── request.json
├── scrape_result.json
├── _COMPLETE.json or _FAILED.json
└── retailer/
    ├── source.md
    ├── product_evidence.json
    ├── product_evidence.md
    ├── claims.md
    ├── vision.md
    ├── quality_report.json
    ├── source_alignment_report.json
    ├── evidence_recovery_report.json
    ├── metadata.json
    ├── images/
    ├── tables/
    └── manifests/
        ├── agent_trace.json
        ├── artifact_manifest.json
        ├── image_manifest.json
        └── table_manifest.json
```

## Crawl4AI multi-profile capture fields

| Field | Meaning |
|---|---|
| `capture_profile_used` | Selected Crawl4AI profile used as primary evidence |
| `capture_profiles_attempted` | Same-URL profiles attempted for the row |
| `capture_score` | 0-100 deterministic score for selected browser capture |
| `capture_grade` | Capture quality grade |
| `capture_decision` | Business-readable capture decision |
| `real_scrape_evidence` | Whether meaningful product-page evidence was captured |
| `is_weak_capture` | Weak/mixed/blocked capture flag |
| `is_block_or_challenge` | Block/challenge/access-denied flag |
| `weak_capture_reasons` | Reasons capture is considered weak |

Configured profiles:

```text
standard
load_wait
full_page_scroll
expand_common_sections
extract_gallery_sources
shadow_iframe
retry_relaxed
```

## Visual evidence statuses

| Status | Meaning | Downstream action |
|---|---|---|
| `final_product_images_available` | Clean product image was downloaded and retained | Suitable for automated coding if other gates pass |
| `unverified_images_retained` | Image file exists, but not fully vision-confirmed | Review recommended |
| `screenshot_fallback_only` | Page screenshot retained as rescue evidence | Manual review required |
| `image_recovery_failed` | Image candidates existed, but no usable image file was retained | Not suitable for automated visual coding |
| `no_image_candidates` | No image candidates were discovered | Not suitable for automated visual coding |

## Semantic enrichment

After artifact creation, batch runs semantic enrichment by default. It updates existing files in-place:

```text
retailer/product_evidence.json
retailer/quality_report.json
retailer/source.md
retailer/product_evidence.md
retailer/claims.md
```

It adds downstream coding guidance under:

```text
quality.semantic_enrichment.identity_verification
quality.semantic_enrichment.feature_evidence_readiness
quality.semantic_enrichment.coding_readiness
```

Skip with:

```bash
--skip-semantic-enrichment
```

## Domain profile learning

Batch mode can learn the best Crawl4AI profile per domain during the current run. This does not introduce search and does not change the URL. It only reorders the same configured Crawl4AI profile list.

Disable while debugging strict profile order:

```bash
--disable-domain-profile-learning
```

## Recommended interpretation

| Condition | Interpretation |
|---|---|
| `success=true` and `real_scrape_evidence=true` | Artifact was created and Crawl4AI captured useful product evidence |
| `success=true` and `real_scrape_evidence=false` | Artifact was created, but capture was weak; review before coding |
| `capture_grade=blocked_or_shell` | HTTP 200 may have happened, but product page was not meaningfully captured |
| `requires_manual_review=true` | Do not feed directly to product coding without inspection |
| `semantic_enrichment.coding_readiness.ready_for_coding=true` | Identity, quality, visual, and review gates passed |
