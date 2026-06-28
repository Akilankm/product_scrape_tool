from pathlib import Path

from product_scraping_agent.models import ImageRef, ScrapeRequest
from product_scraping_agent.pipeline import (
    _visual_evidence_decision,
    _write_visual_evidence_md,
    write_failed_artifact_for_request,
)


def test_visual_status_requires_clean_product_image(tmp_path: Path) -> None:
    img_path = tmp_path / "retailer" / "images" / "candidate.png"
    img_path.parent.mkdir(parents=True)
    img_path.write_bytes(b"fake")
    refs = [ImageRef(url="https://example.com/img.png", local_path=img_path, relevance="unverified_kept", description="RELATED: unverified")]
    decision = _visual_evidence_decision(refs, image_required=True)
    assert decision["visual_evidence_status"] == "unverified_images_retained"
    assert decision["image_downloaded_count"] == 1
    assert decision["clean_product_image_count"] == 0


def test_vision_md_is_never_empty_when_no_images(tmp_path: Path) -> None:
    out_dir = tmp_path / "row" / "retailer"
    vision = out_dir / "vision.md"
    _write_visual_evidence_md(vision, out_dir=out_dir, images=[], decision=_visual_evidence_decision([], image_required=True))
    text = vision.read_text(encoding="utf-8")
    assert "Visual Evidence Summary" in text
    assert "no_image_candidates" in text
    assert "No image file was retained" in text


def test_failed_artifact_finalizes_required_files(tmp_path: Path) -> None:
    request = ScrapeRequest(
        product_url="https://example.com/product/123",
        scrape_id="ROW_FAIL",
        main_text="Toy product",
        ean="1234567890123",
        output_root=tmp_path / "scraped",
    )
    result = write_failed_artifact_for_request(request, error="Synthetic failure")
    root = tmp_path / "scraped" / "ROW_FAIL"
    retailer = root / "retailer"
    assert (root / "request.json").exists()
    assert (root / "scrape_result.json").exists()
    assert (root / "_FAILED.json").exists()
    assert (retailer / "product_evidence.json").exists()
    assert (retailer / "quality_report.json").exists()
    assert (retailer / "vision.md").exists()
    assert result.to_scrape_result().artifact_quality == "insufficient"
    assert result.to_scrape_result().visual_evidence_status == "image_recovery_failed"
