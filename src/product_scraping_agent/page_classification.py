"""Deterministic page-type classification helpers.

These checks do not add evidence or scrape new pages. They classify the already
captured URL/title/text so batch review can separate product-detail pages from
category/search/login/challenge pages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

_PRODUCT_SIGNALS = {
    "brand", "manufacturer", "ean", "gtin", "sku", "mpn", "model", "description",
    "specification", "specifications", "details", "features", "age", "material",
    "dimensions", "contents", "warning", "price", "availability", "add to cart",
    "add basket", "buy now", "product details", "item number",
}
_MARKETPLACE_SIGNALS = {
    "sold by", "seller", "marketplace", "fulfilled by", "ships from", "vendor",
    "merchant", "third party", "third-party", "buy box",
}
_BLOCK_SIGNALS = {
    "access denied", "captcha", "verify you are human", "verifica tu identidad",
    "robot check", "unusual traffic", "enable cookies", "are you a human",
    "temporarily blocked", "forbidden", "cloudflare", "akamai", "distil",
}
_SEARCH_TERMS = {"search", "results", "query", "q", "keyword", "s"}
_CATEGORY_PATH_RE = re.compile(r"(?:^|/)(search|category|categories|catalog|collection|collections|listing|results|plp)(?:/|$)", re.I)
_NON_PDP_PATH_RE = re.compile(r"(?:^|/)(cart|basket|checkout|login|signin|account|help|support|brand|brands)(?:/|$)", re.I)


@dataclass
class PageClassification:
    status: str
    confidence: int
    reasons: list[str] = field(default_factory=list)
    is_product_detail_page: bool = False
    is_category_or_search_page: bool = False
    is_marketplace_page: bool = False
    is_block_or_challenge_page: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "page_classification_status": self.status,
            "page_classification_confidence": self.confidence,
            "page_classification_reasons": "; ".join(dict.fromkeys(self.reasons)),
            "is_product_detail_page": self.is_product_detail_page,
            "is_category_or_search_page": self.is_category_or_search_page,
            "is_marketplace_page": self.is_marketplace_page,
            "is_block_or_challenge_page": self.is_block_or_challenge_page,
        }


def _text_blob(row: dict[str, Any]) -> str:
    return "\n".join(str(row.get(k) or "") for k in (
        "product_url", "final_url", "title", "access_status", "access_issue_reason",
        "weak_capture_reasons", "quality_warnings", "missing_critical_fields",
    )).lower()


def classify_page_from_row(row: dict[str, Any], *, extra_text: str = "") -> PageClassification:
    url = str(row.get("final_url") or row.get("product_url") or "")
    parsed = urlparse(url)
    path = parsed.path or ""
    query = parse_qs(parsed.query or "")
    blob = (_text_blob(row) + "\n" + (extra_text or "").lower())[:120_000]
    reasons: list[str] = []

    block_hits = sorted(s for s in _BLOCK_SIGNALS if s in blob[:40_000])
    is_block = bool(block_hits) or str(row.get("access_status") or "") in {"access_denied", "bot_challenge", "geo_restricted", "rate_limited"}
    if is_block:
        reasons.append("block_or_challenge_signal:" + ",".join(block_hits[:4] or [str(row.get("access_status") or "unknown")]))

    is_category = bool(_CATEGORY_PATH_RE.search(path)) or any(k.lower() in _SEARCH_TERMS for k in query)
    if is_category:
        reasons.append("url_looks_like_search_or_category")
    if _NON_PDP_PATH_RE.search(path):
        is_category = True
        reasons.append("url_looks_like_non_product_page")

    product_signal_count = sum(1 for s in _PRODUCT_SIGNALS if s in blob)
    if product_signal_count >= 5:
        reasons.append(f"product_signals={product_signal_count}")
    elif product_signal_count:
        reasons.append(f"weak_product_signals={product_signal_count}")

    marketplace = any(s in blob[:80_000] for s in _MARKETPLACE_SIGNALS)
    if marketplace:
        reasons.append("marketplace_or_seller_terms_detected")

    real_scrape = str(row.get("real_scrape_evidence") or "").lower() in {"1", "true", "yes"}
    capture_decision = str(row.get("capture_decision") or "")
    title = str(row.get("title") or "").strip()
    has_specific_title = bool(title and title.lower() not in {"amazon.com", "access denied", "forbidden", "captcha"})

    is_pdp = bool(
        not is_block
        and not is_category
        and real_scrape
        and (
            capture_decision in {"rich_product_capture", "usable_product_capture"}
            or product_signal_count >= 5
            or (has_specific_title and product_signal_count >= 3)
        )
    )

    if is_block:
        status = "block_or_challenge_page"
        confidence = 90
    elif is_category:
        status = "category_or_search_or_non_product_page"
        confidence = 80 if product_signal_count < 5 else 60
    elif is_pdp:
        status = "product_detail_page"
        confidence = min(95, 60 + product_signal_count * 5)
    elif real_scrape or product_signal_count:
        status = "ambiguous_product_page"
        confidence = 45 + min(product_signal_count * 4, 25)
    else:
        status = "unknown_or_thin_page"
        confidence = 25

    return PageClassification(
        status=status,
        confidence=confidence,
        reasons=reasons,
        is_product_detail_page=is_pdp,
        is_category_or_search_page=is_category,
        is_marketplace_page=marketplace,
        is_block_or_challenge_page=is_block,
    )


__all__ = ["PageClassification", "classify_page_from_row"]
