"""Runtime configuration for the isolated product scraping agent."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_PREFIX = "PCA_"
_ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str) -> str:
    return os.getenv(_PREFIX + name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(_PREFIX + name)
    return int(raw) if raw is not None and raw.strip() else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(_PREFIX + name)
    return float(raw) if raw is not None and raw.strip() else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(_PREFIX + name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Config:
    """Central settings. Only scraper-relevant settings are retained."""

    output_root: Path = Path(_env("OUTPUT_ROOT", str(_ROOT / "data" / "scraped")))

    # Crawl4AI page renderer.
    scrape_headless: bool = _env_bool("SCRAPE_HEADLESS", True)
    scrape_timeout: float = _env_float("SCRAPE_TIMEOUT", 35.0)
    scrape_wait_until: str = _env("SCRAPE_WAIT_UNTIL", "domcontentloaded")

    # Crawl4AI hardening: run several same-URL capture profiles and select the
    # richest product-page capture. No external scraping service is used.
    scrape_multi_profile_enabled: bool = _env_bool("SCRAPE_MULTI_PROFILE_ENABLED", True)
    scrape_profile_sequence: str = _env(
        "SCRAPE_PROFILE_SEQUENCE",
        "standard,load_wait,full_page_scroll,expand_common_sections,extract_gallery_sources,shadow_iframe,retry_relaxed",
    )
    scrape_profile_early_stop_score: int = _env_int("SCRAPE_PROFILE_EARLY_STOP_SCORE", 82)
    scrape_profile_max_profiles: int = _env_int("SCRAPE_PROFILE_MAX_PROFILES", 7)
    scrape_enable_stealth: bool = _env_bool("SCRAPE_ENABLE_STEALTH", True)
    scrape_user_data_dir: str = _env("SCRAPE_USER_DATA_DIR", "")
    scrape_cookies_file: str = _env("SCRAPE_COOKIES_FILE", "")
    scrape_user_agent: str = _env("SCRAPE_USER_AGENT", "")
    scrape_viewport_width: int = _env_int("SCRAPE_VIEWPORT_WIDTH", 1280)
    scrape_viewport_height: int = _env_int("SCRAPE_VIEWPORT_HEIGHT", 900)

    # Geo/access handling. The scraper never treats geo/access blocks as product absence.
    # Configure authorised proxy/VPN egress explicitly when target-country access is required.
    geo_proxy_enabled: bool = _env_bool("GEO_PROXY_ENABLED", False)
    geo_retry_on_access_block: bool = _env_bool("GEO_RETRY_ON_ACCESS_BLOCK", True)
    default_proxy_url: str = _env("PROXY_URL", "")
    accept_language: str = _env("ACCEPT_LANGUAGE", "")

    # Agentic evidence-building loop.
    agentic_enabled: bool = _env_bool("AGENTIC_ENABLED", True)
    agentic_max_iterations: int = _env_int("AGENTIC_MAX_ITERATIONS", 2)
    strict_product_only: bool = _env_bool("STRICT_PRODUCT_ONLY", True)
    write_raw_debug: bool = _env_bool("WRITE_RAW_DEBUG", False)

    # LLM switches.
    llm_enabled: bool = _env_bool("LLM_ENABLED", True)
    llm_vision_enabled: bool = _env_bool("LLM_VISION_ENABLED", True)
    llm_vision_max_images: int = _env_int("LLM_VISION_MAX_IMAGES", 12)
    llm_vision_detail: str = _env("LLM_VISION_DETAIL", "low")
    relevance_batch_enabled: bool = _env_bool("RELEVANCE_BATCH_ENABLED", True)

    # Image CDN recovery. These settings improve recovery when product gallery
    # images return 403 to plain HTTP clients while the product page itself is accessible.
    image_download_timeout: float = _env_float("IMAGE_DOWNLOAD_TIMEOUT", 20.0)
    image_retry_strategies_enabled: bool = _env_bool("IMAGE_RETRY_STRATEGIES_ENABLED", True)
    image_retry_strip_query: bool = _env_bool("IMAGE_RETRY_STRIP_QUERY", False)
    image_required: bool = _env_bool("IMAGE_REQUIRED", True)
    screenshot_fallback_enabled: bool = _env_bool("SCREENSHOT_FALLBACK_ENABLED", True)
    screenshot_timeout: float = _env_float("SCREENSHOT_TIMEOUT", 25.0)
    screenshot_full_page: bool = _env_bool("SCREENSHOT_FULL_PAGE", False)

    # Azure OpenAI / compatible gateway.
    llm_api_key: str = _env("LLM_API_KEY", "")
    llm_api_version: str = _env("LLM_API_VERSION", "2024-10-21")
    llm_endpoint: str = _env("LLM_ENDPOINT", "")
    llm_deployment: str = _env("LLM_DEPLOYMENT", "gpt-4o")
    llm_consumer_id: str = _env("LLM_CONSUMER_ID", "")
    llm_max_tokens: int = _env_int("LLM_MAX_TOKENS", 4096)
    llm_temperature: float = _env_float("LLM_TEMPERATURE", 0.0)
    llm_connect_timeout: float = _env_float("LLM_CONNECT_TIMEOUT", 15.0)
    llm_read_timeout: float = _env_float("LLM_READ_TIMEOUT", 120.0)
    llm_max_retries: int = _env_int("LLM_MAX_RETRIES", 4)


def get_config() -> Config:
    return Config()


def proxy_url_for_country(country_code: str = "") -> str:
    """Return configured proxy URL for a target country, falling back to PCA_PROXY_URL.

    Examples:
        PCA_PROXY_URL_CZ=http://user:pass@cz-proxy:8080
        PCA_PROXY_URL=http://user:pass@generic-proxy:8080
    """
    cc = (country_code or "").strip().upper()
    if cc:
        country_proxy = os.getenv(f"{_PREFIX}PROXY_URL_{cc}", "").strip()
        if country_proxy:
            return country_proxy
    return os.getenv(f"{_PREFIX}PROXY_URL", "").strip()


def accept_language_for_country(country_code: str = "") -> str:
    """Return configured Accept-Language for target country.

    The agent avoids hard-coded locale assumptions. Configure either:
        PCA_ACCEPT_LANGUAGE_CZ=cs-CZ,cs;q=0.9,en;q=0.7
    or:
        PCA_ACCEPT_LANGUAGE=en-US,en;q=0.9
    """
    cc = (country_code or "").strip().upper()
    if cc:
        value = os.getenv(f"{_PREFIX}ACCEPT_LANGUAGE_{cc}", "").strip()
        if value:
            return value
    return os.getenv(f"{_PREFIX}ACCEPT_LANGUAGE", "").strip()
