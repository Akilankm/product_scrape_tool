from pathlib import Path

from product_scraping_agent import ProductInputContext, ScrapeRequest


def test_scrape_request_optional_context_normalization():
    req = ScrapeRequest(
        product_url="https://example.com/p/1",
        main_text="Toy product",
        ean="EAN: 5702017153647",
        retailer_name="Alza",
        country_code="cz",
        output_root=Path("data/scraped"),
    )
    assert req.ean == "5702017153647"
    assert req.country_code == "CZ"
    assert req.input_context.has_any()
    assert "Toy product" in req.resolved_product_hint()


def test_product_context_empty():
    ctx = ProductInputContext()
    assert not ctx.has_any()
    assert "not provided" in ctx.as_prompt_block()
