from product_scraping_agent.full_scraper import FullPage, merge_full_pages, _coerce_text


class MarkdownGenerationResultLike:
    def __init__(self, raw_markdown):
        self.raw_markdown = raw_markdown


def test_coerce_nested_markdown_generation_result_like():
    assert _coerce_text(MarkdownGenerationResultLike("hello")) == "hello"


def test_merge_full_pages_coerces_non_string_markdown_assignment():
    primary = FullPage(url="https://example.com", raw_markdown="first", raw_html="<p>first</p>")
    extra = FullPage(url="https://example.com")
    # Simulate Crawl4AI v0.8+ object leaking into the field after assignment.
    extra.raw_markdown = MarkdownGenerationResultLike("second")  # type: ignore[assignment]
    extra.raw_html = "<p>second</p>"

    merged = merge_full_pages(primary, extra)

    assert isinstance(merged.raw_markdown, str)
    assert "first" in merged.raw_markdown
    assert "second" in merged.raw_markdown
