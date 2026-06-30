# ADR-0005 — Contract-safe semantic enrichment

Status: Accepted
Date: 2026-06-30

## Context

The product-coding engine asked for richer artifacts: identity verification, feature evidence readiness, provenance, and use/review guidance. Adding new mandatory files would help, but it would also change the artifact contract.

## Decision

Enrich existing files in-place and place deterministic downstream guidance under existing flexible quality sections:

```text
product_evidence.json
  quality.semantic_enrichment

quality_report.json
  semantic_enrichment
```

Markdown readiness sections are prepended to existing markdown dossiers:

```text
retailer/source.md
retailer/product_evidence.md
retailer/claims.md
```

## Consequences

- Downstream coding gets higher-grade evidence without a schema migration.
- Existing readers continue to work.
- Identity, feature-readiness, and coding-readiness signals are centralized.
- Future schema expansion can still happen deliberately through a separate ADR.

## Alternatives considered

| Alternative | Reason rejected |
|---|---|
| Add mandatory `identity.json` | Contract expansion not requested yet |
| Replace `product_evidence.json` top-level schema | Breaks downstream readers |
| Keep enrichment prompt-only | Not deterministic enough for fallback artifacts |
