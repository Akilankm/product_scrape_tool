from product_scraping_agent.full_scraper import FullPage, score_full_page_capture
from product_scraping_agent.models import ScrapeResult
from product_scraping_agent.batch import DEFAULT_BATCH_OUTPUT_COLUMNS


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
    assert score["real_scrape_evidence"] is False
    assert "block_or_challenge_terms" in score["weak_reasons"]


def test_result_and_batch_expose_capture_fields():
    assert "capture_score" in ScrapeResult.model_fields
    assert "capture_profile_used" in DEFAULT_BATCH_OUTPUT_COLUMNS
    assert "real_scrape_evidence" in DEFAULT_BATCH_OUTPUT_COLUMNS
