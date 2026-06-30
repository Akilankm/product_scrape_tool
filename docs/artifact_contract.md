# Artifact Contract

The artifact contract is designed for downstream product coding. It separates operational status, product evidence, visual evidence, source alignment, and quality/readiness decisions.

## Contract summary

```text
Batch output CSV
  -> points to one artifact folder per input row
Artifact folder
  -> contains machine-readable JSON, markdown dossiers, images, tables, and manifests
Product evidence
  -> product-only, provenance-tagged evidence for downstream feature coding
Quality report
  -> readiness gate and review/rescrape guidance
```

## Input contract

Required:

```json
{
  "product_url": "https://retailer.example/product/123"
}
```

Recommended optional context:

```json
{
  "main_text": "source product text",
  "ean": "5702017153647",
  "requested_retailer_name": "Original target retailer",
  "requested_country_code": "Original target country",
  "source_retailer_name": "Actual retailer/source represented by product_url",
  "source_country_code": "Actual country/market represented by product_url",
  "source_url_role": "primary_requested_retailer | alternate_retailer_same_country | alternate_retailer_different_country | same_retailer_different_country | marketplace_fallback | global_fallback | unknown",
  "product_hint": "override context shown to image/planner/evidence LLM",
  "upstream_ai_evidence": "optional AI/search evidence already produced upstream",
  "candidate_snippets": ["optional indexed/search snippets already produced upstream"],
  "search_evidence": [{"source_type": "serp", "title": "", "url": "", "text": ""}]
}
```

Optional context is not search input. It is provenance and recovery context. The scraper never searches.

## Batch CSV schema

### Input and source context

| Column | Meaning |
|---|---|
| `row_number` | Batch row number |
| `input_id` | Stable row/artifact identifier |
| `product_url` | Supplied URL to scrape |
| `main_text` | Optional product identity text |
| `ean` | Optional EAN/GTIN identity hint |
| `requested_retailer_name` | Business target retailer |
| `requested_country_code` | Business target country |
| `source_retailer_name` | Retailer represented by supplied URL |
| `source_country_code` | Country represented by supplied URL |
| `source_url_role` | Primary/fallback/source role |

### Quality and capture status

| Column | Meaning |
|---|---|
| `success` | Artifact was created |
| `artifact_quality` | Strong/usable/partial/insufficient style quality gate |
| `quality_score` | Deterministic quality score |
| `requires_manual_review` | Whether the row should be reviewed before coding |
| `missing_critical_fields` | Critical missing evidence fields |
| `quality_warnings` | Quality warnings |
| `access_status` | Accessible/access_denied/bot_challenge/geo_restricted/rate_limited/fetch_error |
| `browser_visible` | Browser capture saw product-visible signals |
| `product_details_recovered` | Product details were recovered from at least one evidence axis |
| `recovery_status` | Recovery status summary |
| `evidence_axes_used` | Provenance axes used in final evidence |
| `capture_profile_used` | Selected Crawl4AI profile |
| `capture_profiles_attempted` | Profiles attempted against same URL |
| `capture_score` | Numeric capture quality score |
| `capture_grade` | Rich/usable/weak/mixed/blocked style grade |
| `capture_decision` | Final capture decision |
| `real_scrape_evidence` | True when actual page evidence was captured |
| `weak_capture_reasons` | Reasons capture was weak |
| `visual_evidence_status` | Final visual evidence status |
| `image_failure_reason` | Why image recovery failed or degraded |

### Artifact paths

| Column | Meaning |
|---|---|
| `artifact_dir` | Root artifact folder for the row |
| `request_json_path` | Request metadata path |
| `scrape_result_json_path` | Public scrape result path |
| `product_evidence_json_path` | Main structured evidence path |
| `product_evidence_md_path` | Markdown evidence dossier path |
| `claims_md_path` | Compact evidence-backed claim summary path |
| `source_md_path` | Clean product-only source text path |
| `vision_md_path` | Visual observations path |
| `quality_report_path` | Quality/readiness gate path |
| `source_alignment_report_path` | Requested/source alignment report path |
| `image_manifest_path` | Image manifest path |
| `table_manifest_path` | Table manifest path |
| `artifact_manifest_path` | Artifact manifest path |
| `agent_trace_path` | Agentic loop trace path |

## Artifact folder

```text
data/scraped/<input_id>/
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

## `product_evidence.json`

Top-level shape:

```text
product_focus_summary
source_alignment
product_identity
retailer_claims
source_specific_claims
product_only_text_blocks
structured_claims
table_claims
visual_claims
upstream_indexed_claims
url_derived_claims
input_context_claims
discrepancies
gaps
noise_exclusion_summary
quality
```

## Semantic enrichment location

Contract-safe enrichment is nested under existing flexible quality objects.

```text
product_evidence.json
  quality
    semantic_enrichment
      identity_verification
      feature_evidence_readiness
      coding_readiness
      claim_row_enrichment_count

quality_report.json
  semantic_enrichment
    identity_verification
    feature_evidence_readiness
    coding_readiness
```

## Evidence axes

| Axis | Meaning | Typical source |
|---|---|---|
| `T` | Rendered product text | Product title, description, bullets |
| `V` | Visual evidence | Retained image, package observation, visible labels |
| `S` | Structured metadata | JSON-LD, Open Graph, product meta tags |
| `D` | HTML tables | Product specification tables |
| `I` | Input context | Main text, EAN, requested context |
| `U` | URL-derived evidence | URL slug or canonical URL hints |
| `A` | Upstream caller-supplied evidence | Search/AI/indexed snippets passed by caller |

## Quality labels

```text
strong       = rich multi-axis evidence; safe for downstream coding
usable       = sufficient evidence with minor warnings
partial      = usable only with review or supplemental evidence
insufficient = do not code automatically; evidence is too weak
```

## Downstream read order

1. `retailer/quality_report.json`
2. `retailer/product_evidence.json`
3. `retailer/claims.md`
4. `retailer/vision.md`
5. `retailer/source.md`
6. `retailer/manifests/image_manifest.json`
7. `retailer/manifests/table_manifest.json`

## Stability rules

| Rule | Status |
|---|---|
| Existing artifact file names are stable | Required |
| Existing folder layout is stable | Required |
| JSON files must be valid UTF-8 JSON | Required |
| Terminal marker must be `_COMPLETE.json` or `_FAILED.json` | Required |
| Search evidence must be caller-supplied, not discovered by scraper | Required |
| Source-specific commercial claims stay scoped to scraped source | Required |
