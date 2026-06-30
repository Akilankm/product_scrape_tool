# ADR-0002 — Crawl4AI-only same-URL capture

Status: Accepted
Date: 2026-06-30

## Context

The scraper must capture product evidence from retailer product pages while staying compatible with enterprise execution environments. It should not depend on multiple scraping providers or external search systems.

## Decision

Use Crawl4AI as the only scraping backend and run multiple same-URL capture profiles when needed:

```text
standard
load_wait
full_page_scroll
expand_common_sections
extract_gallery_sources
shadow_iframe
retry_relaxed
```

Each profile is scored for real product evidence. The best profile is selected and auxiliary structured/image/table signals may be merged from non-noisy profiles.

## Consequences

- Provider behavior is simpler to reason about.
- No Firecrawl or search backend is required.
- Same URL is preserved throughout capture.
- Dynamic pages have multiple capture strategies without URL discovery.

## Alternatives considered

| Alternative | Reason rejected |
|---|---|
| Firecrawl fallback | Adds provider complexity and different output semantics |
| Search fallback from scraper | Violates URL-in/artifact-out boundary |
| One-pass Crawl4AI only | Too brittle for JS-heavy retailers |
