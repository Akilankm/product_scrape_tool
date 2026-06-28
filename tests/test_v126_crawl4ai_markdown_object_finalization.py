from pathlib import Path

from product_scraping_agent.full_scraper import FullPage, score_full_page_capture
from product_scraping_agent.pipeline import _metadata_payload
from product_scraping_agent.models import ProductInputContext, SourceAlignmentContext, UpstreamEvidenceBundle
from product_scraping_agent.text_utils import coerce_text_payload, safe_text_len, truncate_text


class FakeMarkdownGenerationResult:
    def __init__(self, raw_markdown: str):
        self.raw_markdown = raw_markdown
        self.fit_markdown = raw_markdown[:80]


def test_markdown_generation_result_is_coerced_for_len_and_truncate():
    obj = FakeMarkdownGenerationResult("# Product\n\nThis is a useful product page.")
    assert coerce_text_payload(obj).startswith("# Product")
    assert safe_text_len(obj) == len("# Product\n\nThis is a useful product page.")
    assert truncate_text(obj, 12).startswith("# Product")


def test_metadata_payload_does_not_len_markdown_object(tmp_path: Path):
    page = FullPage(
        url="https://example.com/product",
        final_url="https://example.com/product",
        raw_html="<html><title>Product</title></html>",
        title="Product",
    )
    page.raw_markdown = FakeMarkdownGenerationResult("# Product\n\nEvidence text")  # type: ignore[assignment]
    payload = _metadata_payload(
        page,
        ProductInputContext(main_text="Product"),
        SourceAlignmentContext(source_retailer_name="Example"),
        "Product",
        UpstreamEvidenceBundle(),
    )
    assert payload["counts"]["raw_markdown_chars"] == len("# Product\n\nEvidence text")


def test_capture_scoring_does_not_len_markdown_object():
    page = FullPage(
        url="https://example.com/product",
        status=200,
        success=True,
        access_status="accessible",
        title="Example Product",
        raw_html="<html>Example Product</html>",
        images=[("https://example.com/product.jpg", "Example Product")],
    )
    page.raw_markdown = FakeMarkdownGenerationResult("Example Product with complete details " * 100)  # type: ignore[assignment]
    score = score_full_page_capture(page, product_hint="Example Product")
    assert score["markdown_chars"] > 0
    assert score["score"] >= 0
