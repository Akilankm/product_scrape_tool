from pathlib import Path

from product_scraping_agent.batch import request_from_csv_row, result_to_output_row, stable_scrape_id
from product_scraping_agent.models import ScrapeResult


def test_stable_scrape_id_sanitizes_input_id():
    assert stable_scrape_id(" Product 001 / A ", 1) == "Product-001-A"
    assert stable_scrape_id("", 7) == "row_000007"


def test_request_from_csv_row_supports_recommended_columns(tmp_path):
    row = {
        "input_id": "P001",
        "product_url": "https://example.com/p/1",
        "main_text": "Toy product",
        "ean": "EAN: 1234567890123",
        "requested_retailer_name": "Requested Retailer",
        "requested_country_code": "co",
        "source_retailer_name": "Fallback Retailer",
        "source_country_code": "us",
        "source_url_role": "global fallback",
    }
    req = request_from_csv_row(row, row_number=1, output_root=tmp_path)

    assert req.scrape_id == "P001"
    assert req.product_url == "https://example.com/p/1"
    assert req.ean == "1234567890123"
    assert req.source_alignment.requested_retailer_name == "Requested Retailer"
    assert req.source_alignment.requested_country_code == "CO"
    assert req.source_alignment.source_retailer_name == "Fallback Retailer"
    assert req.source_alignment.source_country_code == "US"
    assert req.source_alignment.source_url_role == "global_fallback"
    assert req.source_alignment.alignment_status == "fallback_source_used"


def test_request_from_csv_row_backward_aliases(tmp_path):
    row = {
        "SERIAL_ID": "S-1",
        "PRODUCT_URL": "https://example.com/p/2",
        "MAIN_TEXT": "Alias toy",
        "EAN": "123",
        "RETAILER": "Retailer A",
        "COUNTRY": "cz",
    }
    req = request_from_csv_row(row, row_number=2, output_root=tmp_path)

    assert req.scrape_id == "S-1"
    assert req.main_text == "Alias toy"
    assert req.input_context.retailer_name == "Retailer A"
    assert req.input_context.country_code == "CZ"


def test_result_to_output_row_has_mapping_paths(tmp_path):
    req = request_from_csv_row(
        {"input_id": "P001", "product_url": "https://example.com/p/1"},
        row_number=1,
        output_root=tmp_path,
    )
    result = ScrapeResult(
        success=True,
        scrape_id="P001",
        product_url=req.product_url,
        output_dir=tmp_path / "P001" / "retailer",
        product_evidence_json_path=tmp_path / "P001" / "retailer" / "product_evidence.json",
        claims_md_path=tmp_path / "P001" / "retailer" / "claims.md",
        quality_report_json_path=tmp_path / "P001" / "retailer" / "quality_report.json",
        artifact_quality="usable",
        quality_score=72,
    )
    row = result_to_output_row(row_number=1, input_id="P001", request=req, result=result)

    assert row["input_id"] == "P001"
    assert row["success"] is True
    assert row["artifact_quality"] == "usable"
    assert row["quality_score"] == 72
    assert row["artifact_dir"].endswith("P001/retailer")
    assert row["product_evidence_json_path"].endswith("product_evidence.json")
