from product_scraping_agent import ScrapeRequest, SourceAlignmentContext


def test_scrape_request_separates_requested_and_source_context():
    req = ScrapeRequest(
        product_url="https://fallback.example/product/1",
        main_text="Toy A",
        ean="123 456 789 0123",
        retailer_name="Requested Retailer",
        country_code="CO",
        source_retailer_name="Fallback Retailer",
        source_country_code="US",
        source_url_role="global_fallback",
    )

    assert req.input_context.retailer_name == "Requested Retailer"
    assert req.input_context.country_code == "CO"
    assert req.source_alignment.requested_retailer_name == "Requested Retailer"
    assert req.source_alignment.source_retailer_name == "Fallback Retailer"
    assert req.source_alignment.alignment_status == "fallback_source_used"
    assert req.source_alignment.product_facts_transfer_allowed is True
    assert req.source_alignment.requested_retailer_claims_allowed is False
    assert req.source_alignment.source_specific_claim_scope == "scraped_source_only"


def test_source_alignment_primary_when_requested_matches_source():
    alignment = SourceAlignmentContext(
        requested_retailer_name="Retailer A",
        requested_country_code="CZ",
        source_retailer_name="Retailer A",
        source_country_code="CZ",
        source_url_role="unknown",
    )

    assert alignment.retailer_match is True
    assert alignment.country_match is True
    assert alignment.alignment_status == "primary_requested_source"
    assert alignment.requested_retailer_claims_allowed is True
