# Batch Product Scraping

The batch runner turns a CSV of already-selected product URLs into one clean product evidence artifact per row.

The scraping agent still does **not** perform search or URL discovery. Search/discovery should produce the product URL first; this batch runner only scrapes each supplied URL and writes a row-to-artifact mapping.

## Minimum input CSV

```csv
input_id,product_url
P001,https://retailer.example/product/123
```

## Recommended input CSV

```csv
input_id,product_url,main_text,ean,requested_retailer_name,requested_country_code,source_retailer_name,source_country_code,source_url_role
P001,https://fallback.example/product/123,Product title,1234567890123,Requested Retailer,CO,Fallback Retailer,US,global_fallback
```

## Column meaning

| Column | Required | Meaning |
|---|---:|---|
| `input_id` | Recommended | Stable row/product id. Used as the scrape/artifact folder id. |
| `product_url` | Yes | The exact URL to scrape. |
| `main_text` | No | Product text/title from the input or URL discovery stage. |
| `ean` | No | EAN/GTIN when available. |
| `requested_retailer_name` | No | Original business/target retailer. |
| `requested_country_code` | No | Original target country code. |
| `source_retailer_name` | No | Actual retailer/source represented by `product_url`. |
| `source_country_code` | No | Country/market of `product_url`. |
| `source_url_role` | No | Role of the URL: `primary_requested_retailer`, `alternate_retailer_same_country`, `alternate_retailer_different_country`, `global_fallback`, etc. |

Backward-compatible aliases are also accepted: `url`, `retailer_name`, `country_code`, `EAN`, `MAIN_TEXT`, `RETAILER`, `COUNTRY`.

## Run

```bash
pdm run python scripts/run_batch_scrape.py \
  --input-csv data/samples/batch_input_sample.csv \
  --output-csv data/batch_scrape_output.csv \
  --summary-json data/batch_scrape_summary.json \
  --output-root data/scraped \
  --max-concurrency 2 \
  --resume
```

Or with PDM script:

```bash
pdm run scrape-batch \
  --input-csv data/samples/batch_input_sample.csv \
  --output-csv data/batch_scrape_output.csv \
  --output-root data/scraped
```

## Worker-based output behavior

Batch mode is worker-based. Rows are processed in parallel up to `--max-concurrency`, and each worker finalizes its own row artifact independently.

A valid final row should have either:

```text
_COMPLETE.json
```

or:

```text
_FAILED.json
```

Even when a row fails, the runner should still write a minimal failure artifact so the downstream audit trail is complete.

A row folder that only contains this is not a valid final state:

```text
request.json
retailer/manifests/
```

That means the process was interrupted or an unexpected failure occurred before finalization.

## Output CSV

The batch output CSV maps each input row to its artifact folder and key evidence files.

Important fields:

| Field | Meaning |
|---|---|
| `artifact_dir` | Folder containing the clean product evidence artifact. |
| `success` | Artifact was created. This does not automatically mean the URL yielded rich product evidence. |
| `artifact_quality` | Deterministic quality gate: `strong`, `usable`, `partial`, `insufficient`, etc. |
| `requires_manual_review` | Whether the artifact should be reviewed before downstream coding. |
| `product_evidence_json_path` | Main machine-readable product evidence artifact. |
| `claims_md_path` | Business-readable claim summary. |
| `vision_md_path` | Visual evidence summary; should never be empty. |
| `quality_report_path` | Deterministic quality gate details. |
| `source_alignment_report_path` | Requested context vs actual scraped source alignment. |
| `evidence_recovery_report_path` | Whether browser capture, URL/input evidence, or upstream evidence was used. |
| `error` | Technical exception if the row failed unexpectedly. |

## Artifact folder per row

For `input_id=P001`, the artifact is written under:

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

## Resume behavior

With `--resume`, the runner appends to the output CSV and skips input IDs already marked `success=true` in the output CSV.

Use `--skip-existing-artifacts` to skip rows whose `artifact_manifest.json` already exists.

## Concurrency

Use conservative concurrency for retailers and LLM gateways:

```bash
--max-concurrency 2
```

Increase only after confirming the target runtime, LLM gateway, and retailer access are stable.

For debugging row-level behavior, run:

```bash
--max-concurrency 1
```

## Crawl4AI multi-profile capture fields

Every row is scraped with the configured same-URL Crawl4AI profile sequence. The runner does not use Firecrawl or any external scraping API.

Additional output columns:

| Field | Meaning |
|---|---|
| `capture_profile_used` | The selected Crawl4AI profile whose capture was used as primary evidence. |
| `capture_profiles_attempted` | All same-URL profiles attempted for this row. |
| `capture_score` | 0–100 deterministic score for the selected browser capture. |
| `capture_grade` | `strong`, `usable`, `mixed_capture`, `weak`, or `blocked_or_shell`. |
| `capture_decision` | Business-readable decision such as `rich_product_capture`, `usable_product_capture`, `mixed_capture_needs_review`, `weak_no_real_product_capture`, `input_url_only_artifact`, or `blocked_shell_capture`. |
| `real_scrape_evidence` | Whether the selected capture contains meaningful product-page evidence beyond input/URL hints. |
| `is_weak_capture` | Boolean flag for weak/mixed/blocked captures. |
| `is_block_or_challenge` | Boolean flag for blocked/challenge/access-denied style captures. |
| `capture_decision_bucket` | Compact grouping: `rich`, `usable`, `mixed_review`, `weak`, `blocked`, or other decision value. |
| `weak_capture_reasons` | Reasons a capture is considered weak, for example thin shell, challenge text, generic title, or few product signals. |

Recommended production interpretation:

| Condition | Interpretation |
|---|---|
| `success=true` and `real_scrape_evidence=true` | Artifact was created and Crawl4AI captured useful product evidence. |
| `success=true` and `real_scrape_evidence=false` | Artifact was created, but the page capture was weak; review before coding. |
| `capture_grade=blocked_or_shell` | HTTP 200 may have happened, but the product page itself was not meaningfully captured. |
| `requires_manual_review=true` | Do not feed directly to coding without inspection. |

Tune profiles in `.env`:

```env
PCA_SCRAPE_MULTI_PROFILE_ENABLED=true
PCA_SCRAPE_PROFILE_SEQUENCE=standard,load_wait,full_page_scroll,expand_common_sections,extract_gallery_sources,shadow_iframe,retry_relaxed
PCA_SCRAPE_PROFILE_EARLY_STOP_SCORE=82
PCA_SCRAPE_PROFILE_MAX_PROFILES=7
PCA_SCRAPE_ENABLE_STEALTH=true
```

## Domain profile learning

Batch mode can learn the best Crawl4AI profile per domain during the current run.

Example: if `shadow_iframe` succeeds for an Amazon URL, later Amazon URLs in the same batch can try `shadow_iframe` first instead of waiting for earlier weak profiles to fail. This does not introduce search and does not change the URL. It only reorders the same configured Crawl4AI profile list.

Disable it when debugging strict profile order:

```bash
pdm run scrape-batch \
  --input-csv data/input.csv \
  --output-csv data/output.csv \
  --disable-domain-profile-learning
```

## Mandatory image evidence columns

The batch output CSV includes visual evidence columns:

```text
image_required
image_candidate_count
image_downloaded_count
final_image_count
vision_described_count
screenshot_fallback_used
visual_evidence_status
image_failure_reason
```

Visual evidence statuses:

| Status | Meaning | Downstream action |
|---|---|---|
| `final_product_images_available` | Clean product image was downloaded and retained. | Suitable for automated coding if other gates pass. |
| `unverified_images_retained` | Image file exists, but not fully vision-confirmed as product image. | Review recommended. |
| `screenshot_fallback_only` | Direct image recovery failed; page screenshot retained as rescue evidence. | Manual review required. |
| `image_recovery_failed` | Image candidates existed, but no usable image file was retained. | Not suitable for automated coding. |
| `no_image_candidates` | No image candidates were discovered. | Not suitable for automated coding. |

Filter `visual_evidence_status != final_product_images_available` to find products that are not ready for downstream automated coding.

## v1.2.6 Crawl4AI markdown object compatibility

Some Crawl4AI versions return markdown as a `MarkdownGenerationResult` object instead of a plain string. The batch runner now coerces these payloads before logging, scoring, metadata writing, artifact writing, and fail-safe finalization so workers do not fail with:

```text
object of type 'MarkdownGenerationResult' has no len()
```
