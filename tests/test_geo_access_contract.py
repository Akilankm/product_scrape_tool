from product_scraping_agent.full_scraper import FullPage, merge_full_pages


def test_fullpage_access_fields_default():
    page = FullPage(url="https://example.com/product")
    assert page.access_status == "unknown"
    assert page.geo_restricted is False
    assert page.proxy_used is False
    assert page.access_attempts == []


def test_merge_prefers_accessible_followup_over_blocked_primary():
    blocked = FullPage(
        url="https://example.com/product",
        access_status="geo_restricted",
        access_issue_type="geo_restricted",
        geo_restricted=True,
        raw_markdown="blocked",
    )
    accessible = FullPage(
        url="https://example.com/product",
        access_status="accessible",
        proxy_used=True,
        proxy_source="configured_country_proxy:CZ",
        raw_markdown="product details",
    )
    merged = merge_full_pages(blocked, accessible)
    assert merged.access_status == "accessible"
    assert merged.proxy_used is True
    assert merged.proxy_source == "configured_country_proxy:CZ"
