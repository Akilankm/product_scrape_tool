"""Product Scraping Agent.

Clean scope:
    input  -> product URL plus optional identity hints
    output -> audited scrape artifact folder

No search/discovery, product coding, reporting, or UI code is included.
"""

from .agent import ProductScrapingAgent
from .models import AgentPlan, EvidenceSourceItem, ImageRef, PlannedScrapeAction, ProductEvidence, ProductInputContext, ScrapeRequest, ScrapeResult, ScrapedProduct, TableRef, UpstreamEvidenceBundle
from .pipeline import make_scrape_id, output_dir_for, scrape_product, slug_from_url

__all__ = [
    "ProductScrapingAgent",
    "ProductInputContext",
    "EvidenceSourceItem",
    "UpstreamEvidenceBundle",
    "ScrapeRequest",
    "ScrapeResult",
    "ScrapedProduct",
    "ImageRef",
    "TableRef",
    "PlannedScrapeAction",
    "AgentPlan",
    "ProductEvidence",
    "scrape_product",
    "make_scrape_id",
    "slug_from_url",
    "output_dir_for",
]
