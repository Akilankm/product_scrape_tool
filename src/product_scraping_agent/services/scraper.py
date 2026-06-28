"""Crawl4AI browser runtime and JSON-LD extraction helpers."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import sys
import threading
from pathlib import Path
from typing import Any

from ..config import Config
from ..log import logger

__all__ = [
    "shutdown_scraper",
    "_extract_jsonld_products",
    "_crawler_arun_many",
    "_CANONICAL_RE",
    "_META_OG_RE",
]

_JSONLD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_META_OG_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](og:[^"\']+|product:[^"\']+|description|keywords)["\']'
    r'[^>]+content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_CANONICAL_RE = re.compile(
    r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']',
    re.IGNORECASE,
)


def _extract_jsonld_products(html: str) -> list[dict[str, Any]]:
    """Extract compact Product/Offer JSON-LD blocks from HTML."""
    out: list[dict[str, Any]] = []
    if not html:
        return out
    for raw in _JSONLD_RE.findall(html):
        raw = raw.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue

        nodes: list[Any] = []
        if isinstance(payload, list):
            nodes.extend(payload)
        elif isinstance(payload, dict):
            graph = payload.get("@graph")
            if isinstance(graph, list):
                nodes.extend(graph)
            else:
                nodes.append(payload)

        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_type = node.get("@type") or node.get("type") or ""
            if isinstance(node_type, list):
                type_text = " ".join(str(t).lower() for t in node_type)
            else:
                type_text = str(node_type).lower()
            if "product" not in type_text and "offer" not in type_text:
                continue
            brand = node.get("brand", "")
            if isinstance(brand, dict):
                brand = brand.get("name", "")
            out.append({
                "type": node_type,
                "name": node.get("name", ""),
                "brand": brand,
                "sku": node.get("sku", ""),
                "mpn": node.get("mpn", ""),
                "gtin": node.get("gtin") or node.get("gtin13") or node.get("gtin12") or node.get("gtin14") or "",
                "description": str(node.get("description", ""))[:700],
                "offers": _compact_offers(node.get("offers")),
            })
    return out


def _compact_offers(offers: Any) -> list[dict[str, Any]]:
    if not offers:
        return []
    if isinstance(offers, dict):
        offers = [offers]
    if not isinstance(offers, list):
        return []
    out: list[dict[str, Any]] = []
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        out.append({
            "price": offer.get("price", ""),
            "priceCurrency": offer.get("priceCurrency", ""),
            "availability": str(offer.get("availability", ""))[-80:],
            "sku": offer.get("sku", ""),
            "url": offer.get("url", ""),
        })
    return out


_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)



def _browser_config_kwargs(kwargs: dict[str, Any]):
    """Create BrowserConfig while tolerating Crawl4AI version differences."""
    from crawl4ai import BrowserConfig

    try:
        sig = inspect.signature(BrowserConfig)
        accepted = {k: v for k, v in kwargs.items() if k in sig.parameters and v not in (None, "")}
    except Exception:
        accepted = {k: v for k, v in kwargs.items() if v not in (None, "")}
    return BrowserConfig(**accepted)


def _load_cookies(path: str) -> list[dict[str, Any]] | None:
    """Load Playwright/Crawl4AI cookies from JSON if provided.

    Accepted shapes: a raw list of cookie dicts or {"cookies": [...]}.
    Invalid files are ignored with a warning so scraping still runs.
    """
    if not path:
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload = payload.get("cookies", [])
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
    except Exception as exc:
        logger.warning("scraper: could not load cookies file {} — {}", path, exc)
    return None

_shared_crawler = None  # type: ignore[var-annotated]
_worker_loop: asyncio.AbstractEventLoop | None = None
_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()
_crawler_start_alock: asyncio.Lock | None = None


def _ensure_worker_loop() -> asyncio.AbstractEventLoop:
    """Create a private browser event loop to avoid Jupyter/Windows loop issues."""
    global _worker_loop, _worker_thread
    if _worker_loop is not None and not _worker_loop.is_closed():
        return _worker_loop

    with _worker_lock:
        if _worker_loop is not None and not _worker_loop.is_closed():
            return _worker_loop
        ready = threading.Event()

        def _run() -> None:
            global _worker_loop
            if sys.platform == "win32":
                loop = asyncio.ProactorEventLoop()
            else:
                loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            _worker_loop = loop
            ready.set()
            try:
                loop.run_forever()
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

        thread = threading.Thread(target=_run, name="product-scraper-browser-loop", daemon=True)
        thread.start()
        ready.wait()
        _worker_thread = thread
        logger.info("scraper: browser worker loop started ({})", type(_worker_loop).__name__)

    assert _worker_loop is not None
    return _worker_loop


async def _run_on_worker(coro):
    loop = _ensure_worker_loop()
    cf_fut = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return await asyncio.wrap_future(cf_fut)
    except asyncio.CancelledError:
        cf_fut.cancel()
        raise


async def _ensure_crawler_inner(cfg: Config):
    global _shared_crawler, _crawler_start_alock
    if _shared_crawler is not None:
        return _shared_crawler
    if _crawler_start_alock is None:
        _crawler_start_alock = asyncio.Lock()

    async with _crawler_start_alock:
        if _shared_crawler is not None:
            return _shared_crawler
        from crawl4ai import AsyncWebCrawler, BrowserConfig

        headers = {}
        if cfg.accept_language:
            headers["Accept-Language"] = cfg.accept_language
        cookies = _load_cookies(cfg.scrape_cookies_file)
        user_data_dir = cfg.scrape_user_data_dir.strip() or None
        browser_cfg = _browser_config_kwargs({
            "browser_type": "chromium",
            "headless": cfg.scrape_headless,
            "verbose": False,
            "text_mode": False,
            "light_mode": True,
            "avoid_ads": True,
            "enable_stealth": cfg.scrape_enable_stealth,
            "user_agent": cfg.scrape_user_agent.strip() or _CHROME_UA,
            "viewport_width": cfg.scrape_viewport_width,
            "viewport_height": cfg.scrape_viewport_height,
            "use_persistent_context": bool(user_data_dir),
            "user_data_dir": user_data_dir,
            "cookies": cookies,
            "headers": headers or None,
        })
        crawler = AsyncWebCrawler(config=browser_cfg)
        await crawler.start()
        _shared_crawler = crawler
        logger.info("scraper: shared Chromium browser started")
    return _shared_crawler


async def _crawler_arun_many(
    cfg: Config,
    urls: list[str],
    run_config: Any,
    *,
    timeout: float | None = None,
) -> list[Any]:
    """Run Crawl4AI's arun_many on the private browser loop."""

    async def _do():
        crawler = await _ensure_crawler_inner(cfg)
        coro = crawler.arun_many(urls=urls, config=run_config)
        if timeout is not None:
            return await asyncio.wait_for(coro, timeout=timeout)
        return await coro

    return await _run_on_worker(_do())


async def shutdown_scraper() -> None:
    """Close the shared Chromium browser if it exists."""
    global _shared_crawler
    crawler = _shared_crawler
    if crawler is None:
        return
    _shared_crawler = None

    async def _close():
        try:
            await crawler.close()
            logger.info("scraper: shared Chromium browser closed")
        except Exception as exc:
            logger.warning("scraper: browser close failed: {}", exc)

    await _run_on_worker(_close())
