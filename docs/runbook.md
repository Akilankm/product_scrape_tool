# Runbook

This runbook helps operators decide what to inspect, when to re-run, and when an artifact is safe for downstream product coding.

## Fast decision table

| Symptom | Likely cause | First check | Action |
|---|---|---|---|
| `success=true` but weak evidence | Artifact finalized, but capture was thin | `quality_report.json` | Use review bucket; do not assume coding-ready |
| `real_scrape_evidence=false` | Page blocked/thin or recovery-only artifact | `scrape_result.json` and `evidence_recovery_report.json` | Re-run with lower concurrency or use upstream evidence |
| `visual_evidence_status=image_recovery_failed` | Image candidates failed or no usable image retained | `vision.md`, `image_manifest.json` | Review/retry; do not auto-code visual-dependent features |
| `screenshot_fallback_only` | Gallery image recovery failed; screenshot retained | `vision.md` | Manual review; lower confidence |
| `requires_manual_review=true` | Quality gate detected risk | `quality_report.json` | Route to review or re-scrape |
| No `_COMPLETE.json` or `_FAILED.json` | Interrupted/unfinalized row | artifact root | Re-run row; inspect logs |
| `identity_status=wrong_item` | EAN/title mismatch | `quality.semantic_enrichment.identity_verification` | Do not code; re-scrape correct URL |

## Recommended review order

```text
1. batch output CSV
2. retailer/quality_report.json
3. retailer/product_evidence.json
4. retailer/claims.md
5. retailer/vision.md
6. retailer/source.md
7. retailer/manifests/image_manifest.json
8. retailer/manifests/table_manifest.json
```

## Quality decisions

| Quality | Meaning | Downstream action |
|---|---|---|
| `strong` | Rich, multi-axis evidence | Automated coding allowed |
| `usable` | Sufficient evidence with minor warnings | Automated coding allowed with caution |
| `partial` | Some useful evidence, missing important parts | Code supported features only; review gaps |
| `insufficient` | Evidence too weak | Manual review or re-scrape |

## Semantic enrichment decisions

Open `retailer/quality_report.json` and inspect:

```text
semantic_enrichment.identity_verification.identity_status
semantic_enrichment.coding_readiness.ready_for_coding
semantic_enrichment.coding_readiness.recommended_downstream_action
semantic_enrichment.feature_evidence_readiness
```

### Identity statuses

| Status | Meaning | Action |
|---|---|---|
| `strong` | EAN or strong title match | Safe identity |
| `medium` | Usable title match | Cautious coding |
| `weak` | Low identity confidence | Review |
| `wrong_item` | Conflicting identity | Do not code |
| `unknown` | Identity not proven | Review or re-scrape |

## Re-run patterns

### Thin or blocked page

```bash
pdm run scrape-batch \
  --input-csv data/retry_rows.csv \
  --output-csv data/retry_output.csv \
  --output-root data/scraped_retry \
  --max-concurrency 1 \
  --max-agent-iterations 2
```

### Need raw debugging

```bash
pdm run scrape-batch \
  --input-csv data/retry_rows.csv \
  --output-csv data/retry_output.csv \
  --output-root data/scraped_debug \
  --write-raw-debug \
  --max-concurrency 1
```

### Check environment before retry

```bash
pdm run runtime-preflight \
  --output-root data/scraped \
  --check-browser-launch
```

## Audit and triage

Run audit after any batch:

```bash
pdm run audit-artifacts \
  --output-root data/scraped \
  --output-csv data/artifact_audit.csv \
  --summary-json data/artifact_audit_summary.json
```

Run triage after batch output CSV is available:

```bash
pdm run triage-batch \
  --input-csv data/batch_scrape_output.csv \
  --output-csv data/batch_triage.csv \
  --summary-json data/batch_triage_summary.json
```

## Never infer these from weak evidence

| Feature area | Rule |
|---|---|
| EAN/GTIN | Require direct evidence or trusted input context |
| Brand/manufacturer | Prefer text/table/metadata evidence; image-only brand marks need caution |
| Battery/electronics | Absence of visible battery compartment is not proof unless evidence says no battery |
| Age recommendation | Require text/table/package observation |
| Material | Do not infer from appearance alone unless package/page states it |
| Piece count | Prefer title/table/package text; image counts can support but not replace text |

## Escalation rule

If the artifact looks strong but identity is weak, treat the row as high risk. The worst failure mode is a wrong-product artifact that looks complete.
