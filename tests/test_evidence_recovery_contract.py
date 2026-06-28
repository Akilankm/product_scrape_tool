from pathlib import Path

from product_scraping_agent.agentic import (
    build_evidence_recovery_report,
    deterministic_product_details_recovered,
    deterministic_product_evidence,
    evidence_axes_from_product_evidence,
)
from product_scraping_agent.full_scraper import FullPage
from product_scraping_agent.models import (
    EvidenceSourceItem,
    ProductInputContext,
    ScrapeRequest,
    ScrapedProduct,
    UpstreamEvidenceBundle,
)


def test_scrape_request_accepts_upstream_evidence():
    req = ScrapeRequest(
        product_url="https://retailer.example/p/1",
        main_text="Toy ABC",
        country_code="co",
        upstream_ai_evidence="Indexed evidence says Toy ABC is a plush toy.",
        candidate_snippets=["Toy ABC - plush toy - retailer.example"],
        search_evidence=[EvidenceSourceItem(source_type="serp", title="Toy ABC", text="Toy ABC product page snippet")],
    )
    assert req.country_code == "CO"
    assert req.upstream_evidence.has_any()
    assert req.upstream_evidence.search_evidence[0].source_type == "serp"


def test_recovery_report_does_not_equate_block_with_absence():
    page = FullPage(
        url="https://retailer.example/p/blocked",
        success=False,
        status=403,
        access_status="geo_restricted",
        access_issue_type="geo_restricted",
        access_issue_reason="blocked from runtime geography",
        geo_restricted=True,
    )
    upstream = UpstreamEvidenceBundle(
        ai_mode_evidence="Retailer indexed snippet: Brand X Product Y, EAN 1234567890123.",
    )
    result = ScrapedProduct(url=page.url, access_status=page.access_status, geo_restricted=True, upstream_evidence=upstream)
    evidence = deterministic_product_evidence(
        page=page,
        tables=[],
        images=[],
        input_context=ProductInputContext(main_text="Brand X Product Y", ean="1234567890123", country_code="CO"),
        product_hint="Brand X Product Y | EAN 1234567890123 | country CO",
        upstream_evidence=upstream,
        reason="test fallback",
    )
    assert deterministic_product_details_recovered(evidence)
    assert "A" in evidence_axes_from_product_evidence(evidence)
    report = build_evidence_recovery_report(result=result, evidence=evidence, upstream_evidence=upstream, page=page)
    assert report["browser_visible"] is False
    assert report["product_details_recovered"] is True
    assert report["recovery_status"] == "upstream_recovery"
    assert "upstream_indexed_search_ai_evidence" in report["recovery_sources"]
