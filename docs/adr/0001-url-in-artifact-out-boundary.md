# ADR-0001 — URL-in / artifact-out boundary

Status: Accepted
Date: 2026-06-30

## Context

The wider product-coding system has separate responsibilities: URL discovery, scraping, rulebook interpretation, product coding, and reporting. Mixing these concerns makes debugging difficult and can hide wrong-product failures.

## Decision

This repository is limited to:

```text
supplied product URL -> verified product evidence artifact
```

The scraper accepts optional identity/source hints, but it does not search for URLs and it does not code product features.

## Consequences

- The runtime is easier to test and audit.
- Search/discovery budgets remain outside this repository.
- Product coding can treat the artifact as evidence, not as a feature decision.
- Wrong URL selection remains a discovery/input problem unless identity validation detects it.

## Alternatives considered

| Alternative | Reason rejected |
|---|---|
| Add URL search inside scraper | Blurs responsibility and makes evidence provenance harder |
| Product-code directly inside scraper | Couples scraping with rulebook logic |
| Accept only URL with no identity hints | Reduces ability to detect wrong-product captures |
