"""Native proxy/locale routing for same-URL scraping retries.

The orchestration logic is built into the scraper. The actual proxy endpoint is
external configuration because it is a credentialed network resource.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .config import Config, accept_language_for_country, proxy_url_for_country
from .models import ProductInputContext
from .url_analysis import URLAnalysis


class ProxyPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    proxy_url: str = ""
    proxy_source: str = "direct"
    target_country_code: str = ""
    accept_language: str = ""
    reason: str = ""
    credential_policy: str = (
        "Proxy orchestration is inbuilt; proxy endpoint/credentials are supplied via request override, env, YAML, or secret injection."
    )


def resolve_proxy_plan(
    cfg: Config,
    *,
    url_analysis: URLAnalysis,
    input_context: ProductInputContext,
    proxy_url_override: str = "",
    proxy_country_code: str = "",
    enable_proxy_retry: bool = True,
) -> ProxyPlan:
    """Resolve the country/locale/proxy plan without treating context as product truth."""
    target_country = (
        (proxy_country_code or "").strip().upper()
        or input_context.country_code.strip().upper()
        or url_analysis.url_country_hint.strip().upper()
    )
    accept_language = accept_language_for_country(target_country)

    proxy_url = (proxy_url_override or "").strip()
    proxy_source = "request_override" if proxy_url else ""
    if not proxy_url:
        proxy_url = proxy_url_for_country(target_country)
        if proxy_url:
            proxy_source = f"configured_country_proxy:{target_country}" if target_country else "configured_proxy"

    enabled = bool(enable_proxy_retry and proxy_url)
    reason_parts = []
    if proxy_country_code:
        reason_parts.append("proxy_country_code override supplied")
    if input_context.country_code:
        reason_parts.append("country_code supplied as routing context")
    if url_analysis.url_country_hint:
        reason_parts.append("URL country hint available")
    if proxy_url_override:
        reason_parts.append("proxy URL override supplied")
    elif proxy_url:
        reason_parts.append("configured proxy endpoint found")
    else:
        reason_parts.append("no proxy endpoint configured; direct scrape only")

    return ProxyPlan(
        enabled=enabled,
        proxy_url=proxy_url,
        proxy_source=proxy_source or "direct",
        target_country_code=target_country,
        accept_language=accept_language,
        reason="; ".join(reason_parts),
    )


__all__ = ["ProxyPlan", "resolve_proxy_plan"]
