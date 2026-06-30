# ADR-0004 — Evidence axes and provenance

Status: Accepted
Date: 2026-06-30

## Context

The product-coding engine needs to know whether a value came from rendered page text, a table, image evidence, structured metadata, caller input, URL hints, or upstream supplied evidence. Without provenance, weak or source-specific values can look stronger than they are.

## Decision

Use explicit evidence axes:

| Axis | Meaning |
|---|---|
| `T` | Rendered product text |
| `V` | Visual evidence |
| `S` | Structured metadata, JSON-LD, meta tags |
| `D` | HTML tables |
| `I` | Input context |
| `U` | URL-derived evidence |
| `A` | Upstream caller-supplied evidence |

Claims should include source refs, confidence, coding relevance, and scope wherever possible.

## Consequences

- Downstream coding can prioritize stronger evidence.
- Unsupported fields can be left missing instead of guessed.
- Input-derived and upstream-derived evidence remain distinguishable from browser-rendered retailer evidence.

## Alternatives considered

| Alternative | Reason rejected |
|---|---|
| Store values without provenance | Not audit-safe |
| Collapse all browser evidence into one axis | Too coarse for feature coding |
| Treat upstream evidence as page evidence | Misrepresents source strength |
