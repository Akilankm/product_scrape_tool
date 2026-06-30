# ADR-0003 — Artifact contract stability

Status: Accepted
Date: 2026-06-30

## Context

Downstream product-coding tools consume the scrape artifact as their primary evidence source. Changing artifact file names or folder layout can break downstream automation.

## Decision

Preserve the artifact file/folder contract. Improvements should enrich existing files unless a deliberate schema-expansion release is approved.

Stable core files:

```text
request.json
scrape_result.json
retailer/source.md
retailer/product_evidence.json
retailer/product_evidence.md
retailer/claims.md
retailer/vision.md
retailer/metadata.json
retailer/quality_report.json
retailer/source_alignment_report.json
retailer/evidence_recovery_report.json
retailer/noise_report.json
retailer/manifests/*
```

## Consequences

- Downstream readers remain stable.
- Contract-safe enrichment can improve intelligence without migration work.
- New mandatory files require explicit ADR and release note.

## Alternatives considered

| Alternative | Reason rejected |
|---|---|
| Add `identity.json` immediately | Useful but changes file contract |
| Move evidence fields to a new schema | Breaks downstream readers |
| Store only markdown artifacts | Reduces machine-readability |
