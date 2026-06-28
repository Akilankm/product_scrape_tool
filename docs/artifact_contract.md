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
  "retailer_name": "Requested retailer name — backward-compatible alias",
  "country_code": "Requested country code — backward-compatible alias",
  "requested_retailer_name": "Original target retailer name",
  "requested_country_code": "Original target country code",
  "source_retailer_name": "Actual retailer/source represented by product_url, if known",
  "source_country_code": "Actual country/market represented by product_url, if known",
  "source_url_role": "primary_requested_retailer | alternate_retailer_same_country | alternate_retailer_different_country | same_retailer_different_country | marketplace_fallback | global_fallback | unknown",
  "product_hint": "override context shown to image/planner/evidence LLM",
  "upstream_ai_evidence": "optional AI/search evidence already produced upstream",
  "candidate_snippets": ["optional indexed/search snippets already produced upstream"],
  "search_evidence": [{"source_type": "serp", "title": "", "url": "", "text": ""}]
}
```

Optional context is not search input. It is used for provenance, image relevance, same-page evidence planning, and product-only normalization. Optional upstream evidence is also not searched by this agent; it is only consumed when already produced by the discovery layer and is tagged as `A` evidence.

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
evidence_recovery_report.json
source_alignment_report.json
quality_report.json
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
- `evidence_recovery_report.json` explains whether browser access was visible, whether product details were recovered, and which evidence axes were used.
- `source_alignment_report.json` separates requested retailer/country from the actual scraped source and scopes fallback-source commercial claims safely.
- `quality_report.json` is a deterministic acceptance gate for downstream product coding. It reports `strong`, `usable`, `partial`, or `insufficient`, plus missing critical fields and warnings.
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

The planner cannot request web search or a different URL. If supplied, upstream AI/search evidence is used only by the evidence normalizer/recovery layer, not by the scraper planner as a search instruction.

## Downstream handoff

For a product coding agent, hand off the full artifact folder. The most important files are:

```text
product_evidence.json
product_evidence.md
claims.md
source.md
metadata.json
source_alignment_report.json
vision.md
images/
tables/
manifests/artifact_manifest.json
```

## Source alignment contract

The input URL may be an alternate or fallback source. Therefore the agent separates:

```text
requested target context  = original retailer/country from the business/search row
actual scraped source     = product_url and optional source retailer/country
```

`source_alignment_report.json` records:

```json
{
  "requested_context": {"retailer_name": "Requested Retailer", "country_code": "CO"},
  "scraped_source": {
    "product_url": "https://fallback.example/product/123",
    "retailer_name": "Fallback Retailer",
    "country_code": "US",
    "source_url_role": "global_fallback"
  },
  "alignment_status": "fallback_source_used",
  "retailer_match": false,
  "country_match": false,
  "product_facts_transfer_allowed": true,
  "requested_retailer_claims_allowed": false,
  "source_specific_claim_scope": "scraped_source_only"
}
```

Claim scoping rule:

| Claim type | Fallback source allowed? | Scope |
|---|---:|---|
| Product identity/facts: brand, product name, EAN/GTIN, manufacturer, features, contents, age range, images | Yes, if evidence-grounded | Product-level |
| Commercial/source-specific: price, availability, delivery, seller, marketplace terms, shipping, ratings | Only for scraped source unless alignment is primary | Source-specific only |

No retailer or country is hardcoded. The model is generic and data-driven from the provided request fields.


## Access / geo restriction contract

The artifact distinguishes product evidence from access failure. If the retailer page is blocked from the runtime geography, the scraper records the condition instead of treating the URL as invalid or the product as absent.

Fields written to `metadata.json`, `scrape_result.json`, `artifact_manifest.json`, and the quality block of `product_evidence.json`:

```json
{
  "access_status": "accessible | geo_restricted | access_denied | bot_challenge | rate_limited | server_error | fetch_error | unknown",
  "access_issue_type": "geo_restricted",
  "access_issue_reason": "HTTP 451 legal/geographic restriction",
  "geo_restricted": true,
  "proxy_used": false,
  "proxy_source": "direct",
  "access_attempts": []
}
```

When `PCA_GEO_PROXY_ENABLED=true` and a proxy is configured through `PCA_PROXY_URL_<COUNTRY>` or `PCA_PROXY_URL`, the same URL is retried through that authorised egress path. Search/discovery is still not performed by this agent.

## Evidence recovery contract

Browser visibility is not the same as product evidence availability. If the browser cannot render the product page but the caller supplies upstream indexed/AI evidence, the agent can still produce a product artifact. The artifact records:

```json
{
  "browser_visible": false,
  "product_details_recovered": true,
  "recovery_status": "upstream_recovery",
  "evidence_axes_used": ["A", "I"],
  "upstream_evidence_present": true
}
```

Allowed axes:

```text
B = direct browser-rendered page
P = proxy/target-country rendered page
T = rendered product text
S = structured metadata/meta tags
J = JSON-LD
D = HTML tables
V = vision/image
A = caller-supplied upstream indexed/search/AI evidence
I = user input context
```

If no browser, metadata, image, table, or upstream evidence is available, the agent must report insufficient evidence and must not invent product facts.


## Quality gate contract

`quality_report.json` is written for every run. It does not add facts; it audits whether the artifact is safe to hand to downstream product coding.

Example:

```json
{
  "artifact_quality": "usable",
  "quality_score": 78,
  "requires_manual_review": false,
  "missing_critical_fields": [],
  "warnings": ["3 image candidate(s) failed with HTTP 403; CDN recovery may be partial"],
  "evidence_axes_used": ["T", "D", "V", "S"],
  "recommended_followups": []
}
```

Quality labels:

```text
strong       = rich multi-axis evidence; safe for downstream coding
usable       = sufficient evidence with minor warnings
partial      = usable only with review or supplemental evidence
insufficient = do not code automatically; evidence is too weak
```

## v1.2.5 mandatory visual evidence

For product identification, `retailer/images/` must contain at least one retained image file for the artifact to be considered usable. If clean gallery image recovery fails, the scraper may write `images/screenshot_fallback.png`, but this is marked as `screenshot_fallback_only` and requires manual review.

`vision.md` must always contain a visual evidence decision table, even when no image could be recovered.
