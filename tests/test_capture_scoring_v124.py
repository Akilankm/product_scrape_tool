from product_scraping_agent.full_scraper import FullPage, score_full_page_capture


def test_zero_content_capture_scores_zero_even_with_image_candidate():
    page = FullPage(
        url="https://example.com/p/1",
        fetch_profile="standard",
        success=True,
        status=200,
        access_status="accessible",
        images=[("https://cdn.example.com/i.jpg", "product")],
        raw_html="",
        raw_markdown="",
    )
    diag = score_full_page_capture(page, product_hint="toy car", ean="12345")
    assert diag["score"] == 0
    assert diag["grade"] == "blocked_or_shell"
    assert diag["real_scrape_evidence"] is False
    assert diag["capture_decision"] == "input_url_only_artifact"
    assert "no_readable_content" in diag["weak_reasons"]


def test_large_capture_with_block_terms_is_mixed_not_strong_real():
    text = ("Product brand manufacturer price details specification toy car " * 2000) + " captcha verify your identity"
    page = FullPage(
        url="https://example.com/p/2",
        fetch_profile="retry_relaxed",
        success=True,
        status=200,
        access_status="accessible",
        raw_markdown=text,
        raw_html="<html>" + text + "</html>",
        images=[(f"https://cdn.example.com/{i}.jpg", "toy") for i in range(10)],
        tables_html=["<table><tr><td>Brand</td><td>ACME</td></tr></table>"],
    )
    diag = score_full_page_capture(page, product_hint="toy car", ean="")
    assert diag["grade"] == "mixed_capture"
    assert diag["real_scrape_evidence"] is True
    assert diag["capture_decision"] == "mixed_capture_needs_review"
