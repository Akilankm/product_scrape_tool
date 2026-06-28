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
  "product_hint": "override context shown to image/planner/evidence LLM",
  "proxy_url": "optional runtime/secret-provided proxy endpoint",
  "proxy_country_code": "optional proxy target override",
  "enable_proxy_retry": true,
  "upstream_ai_evidence": "optional AI/search evidence already produced upstream",
  "candidate_snippets": ["optional indexed/search snippets already produced upstream"],
  "search_evidence": [{"source_type": "serp", "title": "", "url": "", "text": ""}]
}
```

`product_url` is the primary input. Optional context is not search input and is not retailer truth. It is used for decision trace, validation, locale/proxy routing, image relevance, same-page evidence planning, and product-only normalization. Optional upstream evidence is also not searched by this agent; it is only consumed when already produced by the discovery layer and is tagged as `A` evidence.

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
- `debug_raw/` is disabled by default and only exists if explicitly enabled.

## URL-first decision trace

Before scraping, the agent decomposes the URL and writes `url_analysis` to `request.json`, `metadata.json`, `product_evidence.json` quality metadata, and `manifests/agent_trace.json`. It captures:

```text
domain / retailer domain
URL country and language hints
slug tokens
product-id/SKU candidates
main_text vs URL slug overlap
EAN presence in URL
retailer_name vs domain consistency
country_code vs URL hint consistency
```

These signals guide planning only. The URL remains primary.

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
vision.md
images/
tables/
manifests/artifact_manifest.json
```


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

Proxy orchestration is native. The actual proxy endpoint/credentials remain external and can come from request override, `.env`, YAML, AzureML secret injection, or Key Vault-backed environment variables. If `PCA_PROXY_URL_<COUNTRY>` or `PCA_PROXY_URL` is configured, the same URL is retried through that authorised egress path. Search/discovery is still not performed by this agent.

Proxy target priority:

```text
1. proxy_country_code request override
2. country_code supporting context
3. URL country hint from the domain
```

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
