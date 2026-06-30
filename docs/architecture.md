# Architecture

Product Scraping Agent is a **URL-in / artifact-out** runtime. It does not discover URLs and it does not code product features. Its responsibility is to convert a supplied product URL into a high-grade, product-only evidence artifact for downstream coding.

## Design boundary

| Concern | In scope | Out of scope |
|---|---:|---:|
| Supplied product URL scraping | ✅ |  |
| Same-URL dynamic capture | ✅ |  |
| Product-only evidence normalization | ✅ |  |
| Image/table/metadata extraction | ✅ |  |
| Artifact quality/readiness gating | ✅ |  |
| URL search/discovery |  | ❌ |
| Product feature coding |  | ❌ |
| Rulebook interpretation |  | ❌ |
| Reporting spreadsheets |  | ❌ |

## Component flow

```mermaid
flowchart TD
    A[Input CSV or ScrapeRequest] --> B[Batch preflight]
    B --> C[Runtime preflight]
    C --> D[ProductScrapingAgent]
    D --> E[Agentic same-URL fetch loop]
    E --> F[Crawl4AI multi-profile capture]
    F --> G[Capture scoring and best profile selection]
    G --> H[Table extraction]
    G --> I[Image download, dedup, relevance, vision]
    H --> J[Product-only evidence normalization]
    I --> J
    J --> K[Markdown dossiers]
    J --> L[Quality report]
    L --> M[Contract-safe semantic enrichment]
    M --> N[Artifact manifest and terminal marker]
    N --> O[Batch CSV business validation]
    O --> P[Audit / triage / downstream product coding]

    classDef input fill:#eef,stroke:#447;
    classDef runtime fill:#efe,stroke:#474;
    classDef output fill:#ffe,stroke:#774;
    class A,B,C input;
    class D,E,F,G,H,I,J,K,L,M runtime;
    class N,O,P output;
```

## Runtime sequence

```mermaid
sequenceDiagram
    autonumber
    participant User as User / Batch Runner
    participant Preflight as Preflight Checks
    participant Agent as ProductScrapingAgent
    participant Crawl as Crawl4AI Profiles
    participant Evidence as Evidence Normalizer
    participant Images as Image + Vision Stage
    participant Quality as Quality Gate
    participant Enrich as Semantic Enrichment
    participant Artifact as Artifact Folder
    participant CSV as Batch CSV

    User->>Preflight: validate runtime, output root, duplicate input_id
    Preflight-->>User: preflight report
    User->>Agent: ScrapeRequest(product_url + optional hints)
    Agent->>Crawl: run same-URL profile sequence
    Crawl-->>Agent: markdown, html, metadata, tables, images, access attempts
    Agent->>Agent: score captures and select best profile
    Agent->>Images: download, dedup, classify, describe images
    Images-->>Agent: image_manifest + vision.md
    Agent->>Evidence: normalize product-only evidence
    Evidence-->>Agent: product_evidence.json + product_evidence.md
    Agent->>Quality: evaluate identity, content, visual and structured evidence
    Quality-->>Agent: quality_report.json
    Agent->>Enrich: add contract-safe coding-readiness guidance
    Enrich-->>Artifact: update existing files in-place
    Agent->>Artifact: write manifest and _COMPLETE or _FAILED marker
    Agent->>CSV: append row status and artifact paths
```

## Same-URL capture profiles

The scraper can run these capture profiles against the **same supplied URL**:

| Profile | Purpose |
|---|---|
| `standard` | Basic Crawl4AI render and extraction |
| `load_wait` | Longer page-load wait for JS-heavy retailers |
| `full_page_scroll` | Scroll page to trigger lazy-loaded text/images |
| `expand_common_sections` | Click visible product accordions/tabs like specifications, details, safety, manufacturer |
| `extract_gallery_sources` | Stimulate gallery/thumb/carousel nodes to expose product image URLs |
| `shadow_iframe` | Process iframes and shadow DOM when supported |
| `retry_relaxed` | Last-ditch relaxed DOM capture for blocked or thin pages |

Each profile is scored and the best same-URL capture is selected. Auxiliary images, tables, JSON-LD, and metadata may be merged from other non-noisy profiles.

## Per-profile access attempts

```mermaid
flowchart LR
    A[direct_initial] --> B{Transient or blocked?}
    B -- no --> Z[Use result]
    B -- yes --> C[direct_retry]
    C --> D{Geo/access issue and proxy configured?}
    D -- yes --> E[geo_proxy_retry]
    D -- no --> F{Still transient?}
    E --> F
    F -- yes --> G[last_ditch]
    F -- no --> Z
    G --> Z
```

## Evidence axes

Evidence emitted to downstream consumers is provenance-tagged across these axes:

| Axis | Meaning | Example |
|---|---|---|
| `T` | Rendered product text | Product title, description, bullets |
| `V` | Visual evidence | Package image, visible age label, piece count |
| `S` | Structured metadata | JSON-LD, Open Graph, product meta tags |
| `D` | HTML tables | Specification table rows |
| `I` | User input context | Main text, EAN, requested retailer/country |
| `U` | URL-derived evidence | Slug/canonical URL hints |
| `A` | Upstream supplied evidence | Search/AI/indexed snippets passed by caller |

## Artifact lifecycle

```mermaid
stateDiagram-v2
    [*] --> InProgress: write _IN_PROGRESS.json
    InProgress --> Capturing: Crawl4AI profiles
    Capturing --> Evidence: normalize product evidence
    Evidence --> Quality: quality gate
    Quality --> Enrichment: semantic enrichment
    Enrichment --> Complete: write _COMPLETE.json
    InProgress --> Failed: fatal row error
    Capturing --> Failed: no recoverable evidence
    Evidence --> Failed: unrecoverable normalization failure
    Failed --> [*]
    Complete --> [*]
```

## Downstream handoff

The product-coding engine should normally read files in this order:

1. `retailer/quality_report.json`
2. `retailer/product_evidence.json`
3. `retailer/claims.md`
4. `retailer/vision.md`
5. `retailer/source.md`
6. `retailer/manifests/image_manifest.json`
7. `retailer/manifests/table_manifest.json`

## Non-goals

The runtime intentionally does not:

- search Google, SerpAPI, SearXNG, or other engines;
- invent alternate URLs;
- product-code features;
- interpret official rulebooks;
- silently treat fallback-source commercial claims as requested-retailer claims.
