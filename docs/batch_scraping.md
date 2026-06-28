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
| `source_url_role` | No | Role of the URL: `primary_requested_retailer`, `global_fallback`, etc. |

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

## Output CSV

The batch output CSV maps each input row to its artifact folder and key evidence files:

```csv
input_id,product_url,success,artifact_quality,requires_manual_review,artifact_dir,product_evidence_json_path,claims_md_path,quality_report_path,source_alignment_report_path,error
```

Important fields:

| Field | Meaning |
|---|---|
| `artifact_dir` | Folder containing the clean product evidence artifact. |
| `artifact_quality` | Deterministic quality gate: `strong`, `usable`, `partial`, `insufficient`, etc. |
| `requires_manual_review` | Whether the artifact should be reviewed before downstream coding. |
| `product_evidence_json_path` | Main machine-readable product evidence artifact. |
| `claims_md_path` | Business-readable claim summary. |
| `source_alignment_report_path` | Requested context vs actual scraped source alignment. |
| `evidence_recovery_report_path` | Whether browser capture, URL/input evidence, or upstream evidence was used. |

## Artifact folder per row

For `input_id=P001`, the artifact is written under:

```text
data/scraped/P001/
├── request.json
├── scrape_result.json
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
