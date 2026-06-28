from pathlib import Path

from product_scraping_agent import ScrapeRequest, analyze_product_url
from product_scraping_agent.config import Config
from product_scraping_agent.proxy_router import resolve_proxy_plan


def test_url_analysis_is_primary_and_context_is_supporting():
    analysis = analyze_product_url(
        "https://www.alza.cz/lego-duplo-10965-d123456.htm",
        main_text="LEGO DUPLO 10965 Bath Time Fun",
        ean="5702017153647",
        retailer_name="Alza",
        country_code="CZ",
    )
    assert analysis.hostname == "www.alza.cz"
    assert analysis.retailer_domain == "alza.cz"
    assert analysis.url_country_hint == "CZ"
    assert "lego" in analysis.slug_tokens
    assert analysis.supporting_context_assessment["country_code_vs_url"]["status"] == "consistent"
    assert "primary" in analysis.supporting_context_assessment["policy"]


def test_scrape_request_has_proxy_routing_context():
    req = ScrapeRequest(
        product_url="https://www.example.cz/p/abc-123",
        country_code="cz",
        proxy_country_code="cz",
        proxy_url="http://proxy.example:8080",
    )
    assert req.country_code == "CZ"
    assert req.proxy_country_code == "CZ"
    assert req.enable_proxy_retry is True


def test_proxy_plan_request_override_is_native_routing():
    req = ScrapeRequest(
        product_url="https://www.example.cz/p/abc-123",
        country_code="CZ",
        proxy_url="http://proxy.example:8080",
    )
    analysis = analyze_product_url(req.product_url, country_code=req.country_code)
    cfg = Config(output_root=Path("data/scraped"))
    plan = resolve_proxy_plan(
        cfg,
        url_analysis=analysis,
        input_context=req.input_context,
        proxy_url_override=req.proxy_url,
        proxy_country_code=req.proxy_country_code,
        enable_proxy_retry=req.enable_proxy_retry,
    )
    assert plan.enabled is True
    assert plan.proxy_source == "request_override"
    assert plan.target_country_code == "CZ"
