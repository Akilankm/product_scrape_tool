"""URL-first analysis for the product scraping agent.

The product URL is the primary input. Optional fields such as main_text, EAN,
retailer_name, and country_code are treated only as supporting context for
planning, validation, locale/proxy routing, and decision trace.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

from pydantic import BaseModel, ConfigDict, Field

from .text_utils import digits_only

_GENERIC_TLDS = {
    "com", "net", "org", "info", "biz", "io", "ai", "app", "dev", "shop", "store",
    "online", "site", "xyz", "co",  # .co can be country or commercial; keep low confidence.
}
_HOST_PREFIXES = {"www", "m", "mobile", "shop", "store"}
_STOP_TOKENS = {
    "product", "products", "item", "detail", "details", "p", "dp", "sku", "catalog",
    "category", "categories", "en", "es", "fr", "de", "it", "pt", "cs", "pl",
}


class URLAnalysis(BaseModel):
    """Machine-readable URL decomposition and supporting-context assessment."""

    model_config = ConfigDict(extra="forbid")

    input_url: str
    normalized_url: str = ""
    scheme: str = ""
    netloc: str = ""
    hostname: str = ""
    retailer_domain: str = ""
    path: str = ""
    query_keys: list[str] = Field(default_factory=list)
    slug_tokens: list[str] = Field(default_factory=list)
    product_id_candidates: list[str] = Field(default_factory=list)
    url_country_hint: str = ""
    url_country_hint_source: str = ""
    url_language_hint: str = ""
    url_language_hint_source: str = ""
    url_product_signal_summary: str = ""
    url_confidence: str = "medium"
    supporting_context_assessment: dict[str, object] = Field(default_factory=dict)

    def target_country_candidates(self) -> list[str]:
        values: list[str] = []
        for v in [self.url_country_hint]:
            if v and v not in values:
                values.append(v)
        return values


def _host_without_port(host: str) -> str:
    return (host or "").split(":", 1)[0].strip().lower()


def _retailer_domain(hostname: str) -> str:
    parts = [p for p in hostname.split(".") if p]
    while parts and parts[0] in _HOST_PREFIXES:
        parts.pop(0)
    return ".".join(parts) or hostname


def _tokenize_url_text(text: str) -> list[str]:
    text = unquote(text or "").lower()
    raw = re.split(r"[^a-z0-9]+", text)
    tokens: list[str] = []
    for token in raw:
        if not token or token in _STOP_TOKENS or len(token) < 2:
            continue
        if token not in tokens:
            tokens.append(token)
    return tokens[:80]


def _country_hint_from_host(hostname: str) -> tuple[str, str]:
    parts = [p for p in hostname.lower().split(".") if p]
    if not parts:
        return "", ""
    # ccTLD or second-level country pattern like com.mx / co.uk.
    last = parts[-1]
    if len(last) == 2 and last.isalpha():
        # .co is ambiguous; keep it as a hint but call out low confidence.
        return last.upper(), "ccTLD_low_confidence" if last in _GENERIC_TLDS else "ccTLD"
    if len(parts) >= 3 and len(parts[-1]) == 2:
        return parts[-1].upper(), "multi_label_ccTLD"
    return "", ""


def _language_hint_from_url(tokens: list[str], query_keys: list[str]) -> tuple[str, str]:
    # This is deliberately conservative and trace-only. It is not hard-coded product truth.
    known = {"en", "es", "fr", "de", "it", "pt", "cs", "pl", "nl", "tr", "sv", "da", "fi"}
    for key in [*query_keys, *tokens[:8]]:
        key_l = key.lower()
        if key_l in known:
            return key_l, "url_token_or_query"
        if key_l in {"lang", "language", "locale"}:
            return "", "query_key_present"
    return "", ""


def _product_id_candidates(tokens: list[str], query: dict[str, list[str]]) -> list[str]:
    out: list[str] = []
    for token in tokens:
        has_digit = any(ch.isdigit() for ch in token)
        if has_digit and len(token) >= 4 and token not in out:
            out.append(token)
    for key in ("id", "productid", "product_id", "sku", "mpn", "ean", "gtin", "code"):
        for val in query.get(key, []):
            cleaned = re.sub(r"[^A-Za-z0-9_-]+", "", val or "")
            if cleaned and cleaned not in out:
                out.append(cleaned)
    return out[:30]


def _main_text_overlap(main_text: str, slug_tokens: list[str]) -> dict[str, object]:
    if not main_text or not slug_tokens:
        return {"status": "not_available", "overlap_tokens": [], "overlap_ratio": 0.0}
    main_tokens = set(_tokenize_url_text(main_text))
    slug_set = set(slug_tokens)
    overlap = sorted(main_tokens & slug_set)
    denom = max(1, min(len(main_tokens), len(slug_set)))
    ratio = round(len(overlap) / denom, 3)
    return {"status": "evaluated", "overlap_tokens": overlap[:25], "overlap_ratio": ratio}


def _retailer_match(retailer_name: str, hostname: str) -> dict[str, object]:
    if not retailer_name:
        return {"status": "not_available", "match": "unknown"}
    tokens = _tokenize_url_text(retailer_name)
    host_text = hostname.lower()
    matches = [t for t in tokens if t and t in host_text]
    return {
        "status": "evaluated",
        "match": "high" if matches else "low",
        "matched_tokens": matches,
        "policy": "supporting validation only; URL remains the primary input",
    }


def _country_consistency(input_country: str, url_country: str, source: str) -> dict[str, object]:
    cc = (input_country or "").strip().upper()
    uh = (url_country or "").strip().upper()
    if not cc and not uh:
        status = "not_available"
    elif cc and uh and cc == uh:
        status = "consistent"
    elif cc and uh and cc != uh:
        status = "conflict"
    elif cc and not uh:
        status = "input_only"
    else:
        status = "url_only"
    return {
        "status": status,
        "input_country_code": cc,
        "url_country_hint": uh,
        "url_country_hint_source": source,
        "policy": "country_code is supporting context for locale/proxy planning and trace, not product truth",
    }


def _ean_in_url(ean: str, parsed_text: str) -> dict[str, object]:
    e = digits_only(ean)
    if not e:
        return {"status": "not_available", "present_in_url": False}
    return {"status": "evaluated", "present_in_url": e in digits_only(parsed_text), "ean": e}


def analyze_product_url(
    url: str,
    *,
    main_text: str = "",
    ean: str = "",
    retailer_name: str = "",
    country_code: str = "",
) -> URLAnalysis:
    """Analyze the URL as the primary planning anchor."""
    parsed = urlparse((url or "").strip())
    hostname = _host_without_port(parsed.hostname or parsed.netloc)
    normalized_url = parsed.geturl()
    query = parse_qs(parsed.query)
    query_keys = sorted(query.keys())[:40]
    path_text = " ".join([parsed.path or "", parsed.params or "", parsed.query or "", parsed.fragment or ""])
    slug_tokens = _tokenize_url_text(path_text)
    url_country, country_source = _country_hint_from_host(hostname)
    lang_hint, lang_source = _language_hint_from_url(slug_tokens, query_keys)
    candidates = _product_id_candidates(slug_tokens, query)
    confidence = "high" if hostname and (slug_tokens or candidates) else ("medium" if hostname else "low")
    analysis = URLAnalysis(
        input_url=url,
        normalized_url=normalized_url,
        scheme=parsed.scheme or "https",
        netloc=parsed.netloc,
        hostname=hostname,
        retailer_domain=_retailer_domain(hostname),
        path=parsed.path or "",
        query_keys=query_keys,
        slug_tokens=slug_tokens,
        product_id_candidates=candidates,
        url_country_hint=url_country,
        url_country_hint_source=country_source,
        url_language_hint=lang_hint,
        url_language_hint_source=lang_source,
        url_product_signal_summary=(
            f"URL host={hostname or '(missing)'}; slug_tokens={slug_tokens[:12]}; "
            f"product_id_candidates={candidates[:8]}"
        ),
        url_confidence=confidence,
        supporting_context_assessment={
            "main_text_vs_url_slug": _main_text_overlap(main_text, slug_tokens),
            "ean_vs_url": _ean_in_url(ean, path_text),
            "retailer_name_vs_domain": _retailer_match(retailer_name, hostname),
            "country_code_vs_url": _country_consistency(country_code, url_country, country_source),
            "policy": (
                "product_url is the primary input. main_text, EAN, retailer_name, and country_code "
                "are supporting context for planning, validation, proxy/locale routing, and decision trace."
            ),
        },
    )
    return analysis


__all__ = ["URLAnalysis", "analyze_product_url"]
