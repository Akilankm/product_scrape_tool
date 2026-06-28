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
