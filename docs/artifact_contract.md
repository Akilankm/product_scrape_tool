# Artifact Contract

## Input

Required:

```json
{
  "product_url": "https://retailer.example/product/123"
}
```

Optional:

```json
{
  "main_text": "source product text",
  "ean": "5702017153647",
  "retailer_name": "Retailer name",
  "country_code": "CZ",
  "product_hint": "override context shown to image/planner/evidence LLM"
}
```

Optional context is not search input. It is used for provenance, image relevance, same-page evidence planning, and product-only normalization.

## Output folder

```text
<output_root>/<scrape_id>/<retailer_label>/
```

Default `retailer_label` is `retailer`.

## Product-only artifact files

```text
source.md
product_evidence.json
product_evidence.md
claims.md
vision.md
metadata.json
noise_report.json
tables/
images/
manifests/agent_trace.json
manifests/image_manifest.json
manifests/table_manifest.json
manifests/artifact_manifest.json
```

## Artifact semantics

- `product_evidence.json` is the primary downstream file.
- `product_evidence.md` is the human-readable version of the same normalized evidence.
- `claims.md` is generated only from `product_evidence.json` and should not reintroduce page noise.
- `source.md` contains product-only text blocks, not full raw page markdown.
- `noise_report.json` records the exclusion policy without storing raw noisy text.
- `debug_raw/` is disabled by default and only exists if explicitly enabled.

## Agentic same-page loop

The LLM planner may request only these same-product-URL actions:

```text
full_page_scroll
expand_common_sections
extract_gallery_sources
retry_relaxed
stop
```

The planner cannot request web search or a different URL.

## Downstream handoff

For a product coding agent, hand off the full artifact folder. The most important files are:

```text
product_evidence.json
product_evidence.md
claims.md
source.md
metadata.json
vision.md
images/
tables/
manifests/artifact_manifest.json
```
