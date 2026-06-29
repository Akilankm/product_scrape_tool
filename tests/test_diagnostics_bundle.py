import csv
import json

from product_scraping_agent.batch_triage import triage_batch_output_csv
from product_scraping_agent.image_diagnostics import build_image_diagnostics_from_row
from product_scraping_agent.page_classification import classify_page_from_row


def test_page_classification_flags_search_url():
    row = {
        "product_url": "https://example.org/search?q=toy",
        "title": "Search results",
        "real_scrape_evidence": "false",
        "capture_decision": "weak_no_real_product_capture",
    }
    report = classify_page_from_row(row)
    assert report.is_category_or_search_page is True
    assert report.status == "category_or_search_or_non_product_page"


def test_image_diagnostics_buckets_failed_download(tmp_path):
    manifest = tmp_path / "image_manifest.json"
    manifest.write_text(json.dumps({"images": [{"error": "http 403", "download_attempts": [{"strategy": "referer", "status": 403}]}]}), encoding="utf-8")
    row = {
        "image_manifest_path": str(manifest),
        "visual_evidence_status": "image_recovery_failed",
        "image_candidate_count": "1",
        "image_downloaded_count": "0",
        "final_image_count": "0",
    }
    report = build_image_diagnostics_from_row(row)
    assert report["image_failure_bucket"] == "image_fetch_forbidden"
    assert "referer:403" in report["image_attempt_buckets"]


def test_batch_triage_writes_priority_csv_and_summary(tmp_path):
    input_csv = tmp_path / "batch.csv"
    output_csv = tmp_path / "triage.csv"
    summary_json = tmp_path / "summary.json"
    input_csv.write_text(
        "input_id,product_url,main_text,success,artifact_quality,access_status,real_scrape_evidence,capture_decision,visual_evidence_status,final_image_count,image_candidate_count,image_downloaded_count\n"
        "P1,https://example.org/search?q=toy,Toy,true,usable,accessible,false,weak_no_real_product_capture,image_recovery_failed,0,1,0\n",
        encoding="utf-8",
    )

    summary = triage_batch_output_csv(input_csv, output_csv=output_csv, summary_json=summary_json)

    assert summary["total_rows"] == 1
    assert output_csv.exists()
    assert summary_json.exists()
    with output_csv.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["page_classification_status"]
    assert rows[0]["image_failure_bucket"]
