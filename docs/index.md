# Product Scraping Agent Documentation

This documentation is organized for engineering, operations, and downstream product-coding consumers.

## Navigation

| Document | Purpose | Audience |
|---|---|---|
| [README](../README.md) | Quick start, project boundary, current release, command index | Everyone |
| [Architecture](architecture.md) | Component map, Mermaid flowchart, Mermaid sequence diagram, runtime phases | Engineers / reviewers |
| [Usage Guide](usage.md) | Install, env setup, single URL run, batch run, audits, triage, quality checks | Operators / developers |
| [Artifact Contract](artifact_contract.md) | Batch CSV schema, artifact folder schema, evidence schema, semantic enrichment | Downstream product coding |
| [Runbook](runbook.md) | Common failures, review buckets, re-run patterns, quality interpretation | Operators |
| [ADR Index](adr/README.md) | Architecture decision records | Engineering leadership |

## Current contract

```text
Input  : supplied product URL plus optional identity/source hints
Runtime: Crawl4AI-only same-URL capture, no search
Output : product-only evidence artifact folder plus batch CSV
```

## Key guarantees

| Guarantee | Status |
|---|---|
| No URL search/discovery inside scraper | Enforced by design |
| Same artifact folder contract | Preserved through v1.3.5 |
| Product images treated as required for direct automation | Enabled through quality gates |
| Worker-safe row finalization | `_COMPLETE.json` or `_FAILED.json` |
| Contract-safe semantic enrichment | Existing files enriched in-place |
| GitHub-renderable diagrams | Mermaid in `docs/architecture.md` |

## Recommended reading order

1. `README.md`
2. `docs/architecture.md`
3. `docs/artifact_contract.md`
4. `docs/usage.md`
5. `docs/runbook.md`
6. `docs/adr/README.md`
