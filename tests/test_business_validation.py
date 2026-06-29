from product_scraping_agent.business_validation import build_business_validation_report_from_row


def test_business_validation_flags_missing_image():
    row = {
        "input_id": "P1",
        "product_url": "about:blank",
        "main_text": "Toy Car",
        "title": "Toy Car",
        "success": "true",
        "artifact_quality": "usable",
        "access_status": "accessible",
        "real_scrape_evidence": "true",
        "capture_decision": "usable_product_capture",
        "capture_grade": "usable",
        "visual_evidence_status": "image_recovery_failed",
        "final_image_count": "0",
    }
    report = build_business_validation_report_from_row(row)
    assert report["manual_review_bucket"] == "REVIEW_IMAGE_FAILED"
    assert report["visual_success"] is False
