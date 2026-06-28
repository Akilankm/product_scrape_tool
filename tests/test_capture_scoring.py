from product_scraping_agent.full_scraper import FullPage, score_full_page_capture
from product_scraping_agent.models import ScrapeResult
from product_scraping_agent.batch import DEFAULT_BATCH_OUTPUT_COLUMNS, BatchOptions


def test_thin_shell_scores_blocked_or_shell():
    page = FullPage(
        url="https://example.com/p",
        success=True,
        status=200,
        access_status="accessible",
        title="Amazon.com",
        raw_markdown="Sorry, we just need to make sure you're not a robot.",
        raw_html="<html><title>Amazon.com</title>captcha</html>",
    )
    score = score_full_page_capture(page, product_hint="Barbie Ken Doll", ean="194735174539")
    assert score["grade"] == "blocked_or_shell"
    assert score["capture_decision"] in {"blocked_shell_capture", "weak_no_real_product_capture"}
    assert score["real_scrape_evidence"] is False
    assert "block_or_challenge_terms" in score["weak_reasons"]


def test_empty_payload_with_image_candidate_gets_zero_score():
    page = FullPage(
        url="https://example.com/p",
        success=True,
        status=200,
        access_status="accessible",
        title="",
        raw_markdown="",
        raw_html="",
        images=[("https://example.com/logo.png", "logo")],
    )
    score = score_full_page_capture(page, product_hint="1001KARTENA5MINT", ean="761312")
    assert score["score"] == 0
    assert score["grade"] == "blocked_or_shell"
    assert score["real_scrape_evidence"] is False
    assert "image_candidates_without_text_payload" in score["weak_reasons"]


def test_rich_capture_with_incidental_block_terms_is_mixed_not_strong_false():
    md = " ".join(["product brand manufacturer description details specification material dimensions"] * 500)
    md += " enable javascript and cookies "
    page = FullPage(
        url="https://example.com/product/123",
        success=True,
        status=200,
        access_status="accessible",
        title="Artoz 1001 Karten A5 mint",
        raw_markdown=md,
        raw_html="<html>" + ("product details " * 5000) + "</html>",
        images=[(f"https://example.com/{i}.jpg", "product") for i in range(20)],
        tables_html=["<table><tr><td>Brand</td><td>Artoz</td></tr></table>"],
    )
    score = score_full_page_capture(page, product_hint="1001KARTENA5MINT", ean="761312")
    assert score["real_scrape_evidence"] is True
    assert not (score["grade"] == "strong" and score["real_scrape_evidence"] is False)
    if score["grade"] == "mixed_capture":
        assert score["capture_decision"] == "mixed_capture_needs_review"


def test_result_and_batch_expose_capture_fields():
    assert "capture_score" in ScrapeResult.model_fields
    assert "capture_decision" in ScrapeResult.model_fields
    assert "capture_profile_used" in DEFAULT_BATCH_OUTPUT_COLUMNS
    assert "capture_decision" in DEFAULT_BATCH_OUTPUT_COLUMNS
    assert "is_weak_capture" in DEFAULT_BATCH_OUTPUT_COLUMNS
    assert "capture_decision_bucket" in DEFAULT_BATCH_OUTPUT_COLUMNS
    assert "real_scrape_evidence" in DEFAULT_BATCH_OUTPUT_COLUMNS
    assert BatchOptions(output_root=__import__("pathlib").Path("x")).domain_profile_learning is True
