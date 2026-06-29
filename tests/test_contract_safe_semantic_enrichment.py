import json

from product_scraping_agent.semantic_enrichment import enrich_artifact_dir


def test_semantic_enrichment_preserves_file_contract_and_enriches_existing_files(tmp_path):
    row = tmp_path / "ROW_0001"
    retailer = row / "retailer"
    retailer.mkdir(parents=True)
    before_files = {
        "retailer/product_evidence.json",
        "retailer/quality_report.json",
        "retailer/metadata.json",
        "retailer/source.md",
        "retailer/product_evidence.md",
        "retailer/claims.md",
    }
    (retailer / "metadata.json").write_text(json.dumps({
        "input_context": {"main_text": "Bavytoy tube animals 18 pieces", "ean": "8590000000000", "retailer_name": "Alza", "country_code": "CZ"},
        "title": "Bavytoy tube animals 18 pieces",
    }), encoding="utf-8")
    (retailer / "product_evidence.json").write_text(json.dumps({
        "product_focus_summary": "Toy animal figure tube.",
        "source_alignment": {"alignment_status": "primary_requested_source"},
        "product_identity": {
            "product_name": {"value": "Bavytoy tube animals 18 pieces", "evidence_axis": ["T"], "confidence": "high"},
            "brand": {"value": "Bavytoy", "evidence_axis": ["T"], "confidence": "high"},
            "ean_gtin": {"value": "8590000000000", "evidence_axis": ["D"], "confidence": "high"},
        },
        "retailer_claims": [
            {"attribute": "Recommended age", "value": "3+", "evidence_axis": ["D"], "source_refs": ["retailer/tables/table_001.md"], "confidence": "high"},
            {"attribute": "Number of pieces", "value": "18", "evidence_axis": ["T", "V"], "source_refs": ["retailer/source.md > Title"], "confidence": "high"},
        ],
        "source_specific_claims": [],
        "structured_claims": [],
        "table_claims": [],
        "visual_claims": [],
        "url_derived_claims": [],
        "input_context_claims": [],
        "gaps": [],
        "discrepancies": [],
        "quality": {"visual_evidence_status": "final_product_images_available"},
    }), encoding="utf-8")
    (retailer / "quality_report.json").write_text(json.dumps({
        "artifact_quality": "strong",
        "requires_manual_review": False,
        "visual_evidence": {"visual_evidence_status": "final_product_images_available"},
        "recommended_followups": [],
    }), encoding="utf-8")
    for name in ["source.md", "product_evidence.md", "claims.md"]:
        (retailer / name).write_text("# Existing\n\nProduct evidence.\n", encoding="utf-8")

    result = enrich_artifact_dir(row)

    after_files = {str(p.relative_to(row)) for p in row.rglob("*") if p.is_file()}
    assert after_files == before_files
    assert result.changed_files
    evidence = json.loads((retailer / "product_evidence.json").read_text(encoding="utf-8"))
    quality = json.loads((retailer / "quality_report.json").read_text(encoding="utf-8"))
    assert evidence["quality"]["semantic_enrichment"]["identity_verification"]["identity_status"] == "strong"
    assert quality["semantic_enrichment"]["coding_readiness"]["ready_for_coding"] is True
    assert evidence["retailer_claims"][0]["coding_relevance"] == "age_range"
    assert "Downstream Product-Coding Readiness" in (retailer / "claims.md").read_text(encoding="utf-8")
