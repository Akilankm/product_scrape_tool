"""Full-content Crawl4AI renderer for one retailer product URL.

This module deliberately stays search-free. It can run multiple *same URL*
profiles so the LLM planner can ask for richer capture when the initial DOM is
incomplete: full-page scroll, common accordion expansion, gallery-source probe,
or a relaxed last-ditch DOM grab.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

from pydantic import BaseModel, ConfigDict, Field

from .config import Config, accept_language_for_country, proxy_url_for_country
from .log import logger
from .services.scraper import (
    _CANONICAL_RE,
    _META_OG_RE,
    _crawler_arun_many,
    _extract_jsonld_products,
)


_IMAGE_ATTRS = (
    "src", "data-src", "data-original", "data-lazy-src", "data-srcset", "srcset",
    "data-zoom-image", "data-large", "data-image", "data-full", "data-full-src",
    "data-hires", "data-high-res-src", "data-original-src", "data-main-image",
    "data-product-image", "content", "href", "poster",
)
_IMAGE_EXT_RE = re.compile(r"https?://[^\s'\")<>]+?\.(?:jpg|jpeg|png|webp|gif)(?:\?[^\s'\")<>]*)?", re.I)
_CSS_URL_RE = re.compile(r"url\((['\"]?)(.*?)\1\)", re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


_ACCESS_BLOCK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("geo_restricted", re.compile(r"\b(not available|unavailable|blocked|restricted)\b.{0,80}\b(country|region|location|territory|area|geo)", re.I)),
    ("geo_restricted", re.compile(r"\b(country|region|location|territory|area|geo)\b.{0,80}\b(not supported|not allowed|not available|blocked|restricted)", re.I)),
    ("access_denied", re.compile(r"\b(access denied|forbidden|request blocked|you don't have permission|permission denied)\b", re.I)),
    ("bot_challenge", re.compile(r"\b(captcha|cloudflare|verify you are human|checking your browser|robot check|bot protection)\b", re.I)),
    ("rate_limited", re.compile(r"\b(too many requests|rate limit|temporarily blocked)\b", re.I)),
)


def _classify_access_issue(status: int, html: str = "", markdown: str = "", error: str = "") -> tuple[str, str, bool]:
    """Classify access failures without claiming product absence."""
    text = "\n".join([error or "", markdown or "", html or ""])[:80_000]
    if status == 451:
        return "geo_restricted", "HTTP 451 legal/geographic restriction", True
    if status in {401, 403}:
        # 403 can be geo, anti-bot, or auth; inspect text before choosing.
        for issue_type, pattern in _ACCESS_BLOCK_PATTERNS:
            if pattern.search(text):
                return issue_type, f"HTTP {status} with {issue_type} indicators", issue_type == "geo_restricted"
        return "access_denied", f"HTTP {status} access denied", False
    if status == 429:
        return "rate_limited", "HTTP 429 rate limited", False
    if 500 <= status < 600:
        return "server_error", f"HTTP {status} server error", False
    for issue_type, pattern in _ACCESS_BLOCK_PATTERNS:
        if pattern.search(text):
            return issue_type, f"page text contains {issue_type} indicators", issue_type == "geo_restricted"
    if error and not (html or markdown):
        return "fetch_error", error[:300], False
    return "none", "", False


def _proxy_label(proxy_url: str, country_code: str = "") -> str:
    if not proxy_url:
        return "direct"
    cc = (country_code or "").strip().upper()
    return f"configured_country_proxy:{cc}" if cc else "configured_proxy"

class FullPage(BaseModel):
    """Rendered page payload and extracted browser-level signals."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    url: str
    final_url: str = ""
    fetch_profile: str = "standard"
    success: bool = False
    status: int = 0
    error: str = ""
    access_status: str = "unknown"
    access_issue_type: str = ""
    access_issue_reason: str = ""
    geo_restricted: bool = False
    proxy_used: bool = False
    proxy_source: str = ""
    access_attempts: list[dict[str, object]] = Field(default_factory=list)
    title: str = ""
    description: str = ""
    canonical_url: str = ""
    raw_html: str = ""
    raw_markdown: str = ""
    og: dict[str, str] = Field(default_factory=dict)
    product_meta: dict[str, str] = Field(default_factory=dict)
    json_ld: list[dict] = Field(default_factory=list)
    images: list[tuple[str, str]] = Field(default_factory=list)
    tables_html: list[str] = Field(default_factory=list)
    profiles_merged: list[str] = Field(default_factory=list)


class _Walker(HTMLParser):
    """Extract image URLs and table HTML in document order."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.images: list[tuple[str, str]] = []
        self.tables_html: list[str] = []
        self._table_depth = 0
        self._table_buf: list[str] = []
        self._seen_imgs: set[str] = set()
        self._last_alt = ""

    def _emit(self, frag: str) -> None:
        if self._table_depth > 0:
            self._table_buf.append(frag)

    def _add_image(self, raw: str | None, alt: str = "") -> None:
        if not raw:
            return
        candidates: list[str] = []
        raw = raw.strip()
        if "," in raw and " " in raw:
            # srcset: keep every URL, not just first; high-res variants often appear later.
            for part in raw.split(","):
                u = part.strip().split(" ")[0].strip()
                if u:
                    candidates.append(u)
        else:
            candidates.append(raw.split(" ")[0].strip())
        for cand in candidates:
            if not cand or cand.startswith("data:"):
                continue
            abs_url = urljoin(self.base_url, cand)
            if abs_url.startswith(("http://", "https://")) and abs_url not in self._seen_imgs:
                self._seen_imgs.add(abs_url)
                self.images.append((abs_url, alt or self._last_alt or ""))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = {k.lower(): (v or "") for k, v in attrs}
        if tag == "table":
            if self._table_depth == 0:
                self._table_buf = []
            self._table_depth += 1
            self._emit(self._render_tag("table", attrs))
            return
        if self._table_depth > 0:
            self._emit(self._render_tag(tag, attrs))

        alt = attrs_d.get("alt") or attrs_d.get("title") or attrs_d.get("aria-label") or ""
        if alt:
            self._last_alt = alt

        # img/source/picture/link/meta/poster and custom gallery attributes.
        if tag in {"img", "source", "meta", "link", "video", "a", "div", "button"}:
            for attr in _IMAGE_ATTRS:
                value = attrs_d.get(attr)
                if value:
                    self._add_image(value, alt)

        style = attrs_d.get("style") or ""
        if "url(" in style.lower():
            for _, raw_url in _CSS_URL_RE.findall(style):
                self._add_image(raw_url, alt)

    def handle_endtag(self, tag: str) -> None:
        if self._table_depth > 0:
            self._emit(f"</{tag}>")
        if tag == "table" and self._table_depth > 0:
            self._table_depth -= 1
            if self._table_depth == 0 and self._table_buf:
                self.tables_html.append("".join(self._table_buf))
                self._table_buf = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag != "img":
            self._emit(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self._emit(data)

    @staticmethod
    def _render_tag(tag: str, attrs: list[tuple[str, str | None]]) -> str:
        parts = [tag]
        for key, value in attrs:
            if value is None:
                parts.append(key)
            else:
                parts.append(f'{key}="{value.replace(chr(34), "&quot;")}"')
        return "<" + " ".join(parts) + ">"




def _coerce_text(value, *, max_chars: int | None = None, _depth: int = 0) -> str:
    """Return plain text from Crawl4AI/Pydantic result variants.

    Crawl4AI changed markdown payload shapes across versions. In some versions
    ``result.markdown`` is already a string; in others it is a
    MarkdownGenerationResult object containing fields such as raw_markdown,
    fit_markdown, markdown, or text. This helper recursively normalizes those
    shapes so the rest of the scraper can safely treat page text/html as str.
    """
    if value is None or _depth > 5:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    elif isinstance(value, (int, float, bool)):
        text = str(value)
    elif isinstance(value, dict):
        text = ""
        for key in (
            "raw_markdown",
            "fit_markdown",
            "markdown",
            "markdown_with_citations",
            "text",
            "content",
            "cleaned_html",
            "html",
        ):
            if key in value:
                text = _coerce_text(value.get(key), max_chars=max_chars, _depth=_depth + 1)
                if text:
                    break
    elif isinstance(value, (list, tuple)):
        parts = [_coerce_text(v, _depth=_depth + 1) for v in value]
        text = "\n".join(p for p in parts if p)
    else:
        text = ""
        for attr in (
            "raw_markdown",
            "fit_markdown",
            "markdown",
            "markdown_with_citations",
            "text",
            "content",
            "cleaned_html",
            "html",
        ):
            try:
                nested = getattr(value, attr)
            except Exception:
                continue
            if nested is value:
                continue
            text = _coerce_text(nested, max_chars=max_chars, _depth=_depth + 1)
            if text:
                break
        if not text:
            try:
                dumped = value.model_dump()  # pydantic v2 models
            except Exception:
                dumped = None
            if isinstance(dumped, dict):
                text = _coerce_text(dumped, max_chars=max_chars, _depth=_depth + 1)
        if not text:
            # Last resort: keep the scraper alive, but avoid noisy object reprs
            # unless the object actually renders to useful text.
            rendered = str(value)
            if "MarkdownGenerationResult" not in rendered and not rendered.startswith("<"):
                text = rendered
    text = text or ""
    return text[:max_chars] if max_chars and len(text) > max_chars else text


def _extract_head_meta(html: str) -> dict[str, object]:
    out: dict[str, object] = {
        "og": {},
        "product_meta": {},
        "title": "",
        "description": "",
        "canonical_url": "",
    }
    if not html:
        return out
    title = _TITLE_RE.search(html)
    if title:
        out["title"] = re.sub(r"\s+", " ", title.group(1)).strip()
    canonical = _CANONICAL_RE.search(html)
    if canonical:
        out["canonical_url"] = canonical.group(1)
    for prop, content in _META_OG_RE.findall(html):
        key = prop.lower()
        if key.startswith("og:"):
            out["og"][key] = content  # type: ignore[index]
        elif key.startswith("product:"):
            out["product_meta"][key] = content  # type: ignore[index]
        elif key == "description" and not out["description"]:
            out["description"] = content
    return out


def _extract_regex_images(html: str, base_url: str) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    if not html:
        return out
    for match in _IMAGE_EXT_RE.findall(html):
        abs_url = urljoin(base_url, match)
        if abs_url not in seen:
            seen.add(abs_url)
            out.append((abs_url, "regex/image-url"))
    return out


def _crawler_config_kwargs(kwargs: dict):
    """Create CrawlerRunConfig while tolerating Crawl4AI version differences."""
    from crawl4ai import CrawlerRunConfig

    try:
        sig = inspect.signature(CrawlerRunConfig)
        accepted = {k: v for k, v in kwargs.items() if k in sig.parameters}
    except Exception:
        accepted = kwargs
    return CrawlerRunConfig(**accepted)


def _profile_js(profile: str) -> str | None:
    if profile not in {"expand_common_sections", "extract_gallery_sources"}:
        return None
    return r"""
(async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const textRe = /(show more|read more|more info|details|description|specification|specifications|parameters|technical|product information|features|manufacturer|safety|see more|expand|weiter|mehr|více|parametry|popis|specifikace|detaily)/i;
  const nodes = Array.from(document.querySelectorAll('button,a,[role="button"],[aria-expanded="false"],summary'));
  let clicked = 0;
  for (const el of nodes) {
    const label = `${el.innerText || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''}`.trim();
    const rect = el.getBoundingClientRect();
    const visible = rect.width > 0 && rect.height > 0;
    if (visible && textRe.test(label) && clicked < 24) {
      try { el.click(); clicked++; await sleep(250); } catch(e) {}
    }
  }
  for (let y = 0; y <= document.body.scrollHeight; y += Math.max(400, Math.floor(window.innerHeight * 0.8))) {
    window.scrollTo(0, y); await sleep(120);
  }
  window.scrollTo(0, 0); await sleep(300);
})();
"""


async def fetch_full(cfg: Config, url: str, *, profile: str = "standard", country_code: str = "") -> FullPage:
    """Render a product URL with Crawl4AI and extract text/html/images/tables."""
    try:
        from crawl4ai import CacheMode
    except ImportError as exc:
        return FullPage(url=url, fetch_profile=profile, error=f"crawl4ai import failed: {exc}")

    base_timeout_ms = int(cfg.scrape_timeout * 1000)
    env_scan_full = os.getenv("PCA_SCAN_FULL_PAGE", "0").lower() in {"1", "true", "yes"}
    scan_full = env_scan_full or profile in {"full_page_scroll", "expand_common_sections", "extract_gallery_sources"}

    def _mk_run_cfg(timeout_ms: int, *, last_ditch: bool = False, proxy_url: str = ""):
        kwargs = {
            "cache_mode": CacheMode.BYPASS,
            "page_timeout": timeout_ms,
            "wait_until": "domcontentloaded" if last_ditch else cfg.scrape_wait_until,
            "delay_before_return_html": 2.0 if profile != "standard" else (1.5 if last_ditch else 0.5),
            "word_count_threshold": 5,
            "exclude_external_links": False,
            "remove_overlay_elements": not last_ditch,
            "verbose": False,
            "scan_full_page": False if last_ditch else scan_full,
            "screenshot": False,
        }
        if proxy_url:
            kwargs["proxy_config"] = proxy_url
        lang = accept_language_for_country(country_code)
        if lang:
            kwargs["headers"] = {"Accept-Language": lang}
        js = _profile_js(profile)
        if js and not last_ditch:
            kwargs["js_code"] = js
        return _crawler_config_kwargs(kwargs)

    async def _attempt(timeout_ms: int, *, last_ditch: bool = False, proxy_url: str = ""):
        try:
            return await _crawler_arun_many(
                cfg,
                [url],
                _mk_run_cfg(timeout_ms, last_ditch=last_ditch, proxy_url=proxy_url),
                timeout=(timeout_ms / 1000.0) * 1.8,
            )
        except asyncio.TimeoutError:
            logger.warning("full_scraper[{}]: hard timeout for {}", profile, url)
            return []
        except Exception as exc:
            logger.warning("full_scraper[{}]: crawl failed for {} — {}", profile, url, exc)
            return []

    access_attempts: list[dict[str, object]] = []

    def _result_texts(r) -> tuple[int, str, str, str, bool]:
        if r is None:
            return 0, "", "", "no crawl result", False
        status = int(getattr(r, "status_code", 0) or 0)
        md_text = _coerce_text(getattr(r, "markdown", None)) or _coerce_text(getattr(r, "raw_markdown", None)) or _coerce_text(getattr(r, "fit_markdown", None))
        html_text = _coerce_text(getattr(r, "cleaned_html", None)) or _coerce_text(getattr(r, "html", None))
        error_text = str(getattr(r, "error_message", "") or "")
        success = bool(getattr(r, "success", False))
        return status, html_text, md_text, error_text, success

    def _transient(r) -> bool:
        if r is None:
            return True
        status, html_text, md_text, _error, success = _result_texts(r)
        if status in {401, 403, 408, 429, 451} or 500 <= status < 600:
            return True
        if not success and not (html_text or md_text):
            return True
        return False

    def _append_attempt(name: str, r, *, proxy_url: str = "") -> None:
        status, html_text, md_text, error_text, success = _result_texts(r)
        issue_type, reason, is_geo = _classify_access_issue(status, html_text, md_text, error_text)
        access_attempts.append({
            "attempt": name,
            "profile": profile,
            "proxy_used": bool(proxy_url),
            "proxy_source": _proxy_label(proxy_url, country_code),
            "target_country_code": (country_code or "").strip().upper(),
            "status": status,
            "success": success,
            "html_chars": len(html_text or ""),
            "markdown_chars": len(md_text or ""),
            "access_issue_type": issue_type,
            "access_issue_reason": reason,
            "geo_restricted": is_geo,
        })

    direct_timeout = base_timeout_ms if profile == "standard" else int(base_timeout_ms * 1.6)
    results = await _attempt(direct_timeout)
    result = results[0] if results else None
    _append_attempt("direct_initial", result)

    if _transient(result):
        logger.warning("full_scraper[{}]: retrying transient fetch for {}", profile, url)
        results = await _attempt(base_timeout_ms * 2)
        retry_result = results[0] if results else None
        _append_attempt("direct_retry", retry_result)
        result = retry_result or result

    # Geo/access-aware escalation: direct failure must not be interpreted as product absence.
    status, html_text, md_text, error_text, _success = _result_texts(result)
    issue_type, reason, is_geo = _classify_access_issue(status, html_text, md_text, error_text)
    proxy_url = proxy_url_for_country(country_code) if cfg.geo_proxy_enabled else ""
    should_proxy_retry = (
        cfg.geo_retry_on_access_block
        and bool(proxy_url)
        and issue_type in {"geo_restricted", "access_denied", "bot_challenge", "rate_limited", "fetch_error"}
    )
    if should_proxy_retry:
        logger.warning(
            "full_scraper[{}]: access issue detected ({}); retrying with configured target-country proxy ({})",
            profile, issue_type, _proxy_label(proxy_url, country_code),
        )
        results = await _attempt(base_timeout_ms * 2, proxy_url=proxy_url)
        proxy_result = results[0] if results else None
        _append_attempt("geo_proxy_retry", proxy_result, proxy_url=proxy_url)
        p_status, p_html, p_md, p_error, p_success = _result_texts(proxy_result)
        p_issue, _p_reason, _p_geo = _classify_access_issue(p_status, p_html, p_md, p_error)
        if proxy_result is not None and (p_success or p_html or p_md) and p_issue not in {"geo_restricted", "access_denied", "bot_challenge"}:
            result = proxy_result

    if _transient(result) or profile == "retry_relaxed":
        logger.warning("full_scraper[{}]: last-ditch DOM grab for {}", profile, url)
        # Use the configured proxy for the relaxed grab only if direct access already showed an access issue.
        relaxed_proxy = proxy_url if proxy_url and issue_type in {"geo_restricted", "access_denied", "bot_challenge"} else ""
        results = await _attempt(15_000, last_ditch=True, proxy_url=relaxed_proxy)
        relaxed_result = results[0] if results else None
        _append_attempt("last_ditch", relaxed_result, proxy_url=relaxed_proxy)
        if relaxed_result is not None:
            result = relaxed_result

    out = FullPage(url=url, fetch_profile=profile, profiles_merged=[profile], access_attempts=access_attempts)
    if result is None:
        out.error = "no crawl result"
        out.access_status = "fetch_failed"
        out.access_issue_type = "fetch_error"
        out.access_issue_reason = "no crawl result"
        return out

    out.success = bool(getattr(result, "success", False))
    out.status = int(getattr(result, "status_code", 0) or 0)
    out.error = str(getattr(result, "error_message", "") or "")
    out.final_url = getattr(result, "url", "") or getattr(result, "redirected_url", "") or url

    # Classify access status. This prevents geo/access blocks being misread as product absence.
    _status, _html_preview, _md_preview, _err_preview, _ = _result_texts(result)
    issue_type, issue_reason, is_geo = _classify_access_issue(_status, _html_preview, _md_preview, _err_preview)
    out.access_issue_type = issue_type
    out.access_issue_reason = issue_reason
    out.geo_restricted = is_geo
    out.access_status = "accessible" if issue_type == "none" and (out.success or _html_preview or _md_preview) else issue_type
    chosen_attempt = access_attempts[-1] if access_attempts else {}
    out.proxy_used = bool(chosen_attempt.get("proxy_used", False))
    out.proxy_source = str(chosen_attempt.get("proxy_source", "direct"))

    md_obj = getattr(result, "markdown", None)
    out.raw_markdown = (
        _coerce_text(md_obj)
        or _coerce_text(getattr(result, "raw_markdown", None))
        or _coerce_text(getattr(result, "fit_markdown", None))
    )

    out.raw_html = _coerce_text(getattr(result, "cleaned_html", None)) or _coerce_text(getattr(result, "html", None))

    meta = getattr(result, "metadata", None) or {}
    if isinstance(meta, dict):
        out.title = meta.get("title", "") or ""
        out.description = meta.get("description", "") or ""
        for key, value in meta.items():
            if not isinstance(key, str) or not isinstance(value, (str, int, float)):
                continue
            kl = key.lower()
            if kl.startswith("og:"):
                out.og[kl] = str(value)
            elif kl.startswith("product:"):
                out.product_meta[kl] = str(value)

    if out.raw_html:
        head = _extract_head_meta(out.raw_html)
        out.title = out.title or str(head["title"])
        out.description = out.description or str(head["description"])
        out.canonical_url = out.canonical_url or str(head["canonical_url"])
        out.og.update(head["og"])  # type: ignore[arg-type]
        out.product_meta.update(head["product_meta"])  # type: ignore[arg-type]
        out.json_ld = _extract_jsonld_products(out.raw_html)
        walker = _Walker(base_url=out.final_url or url)
        try:
            walker.feed(out.raw_html)
        except Exception as exc:
            logger.debug("full_scraper[{}]: HTML walk error: {}", profile, exc)
        merged_imgs: dict[str, str] = {}
        for src, alt in list(walker.images) + _extract_regex_images(out.raw_html, out.final_url or url):
            merged_imgs.setdefault(src, alt)
        # og:image is often the hero product image.
        for key, value in out.og.items():
            if key.endswith("image") and value:
                merged_imgs.setdefault(urljoin(out.final_url or url, value), "og:image")
        out.images = list(merged_imgs.items())
        out.tables_html = walker.tables_html

    logger.info(
        "full_scraper[{}]: status={} access={} proxy={} html={}KB md={}KB images={} tables={} json_ld={}",
        profile,
        out.status,
        out.access_status,
        out.proxy_source or "direct",
        len(out.raw_html) // 1024,
        len(out.raw_markdown) // 1024,
        len(out.images),
        len(out.tables_html),
        len(out.json_ld),
    )
    return out


def merge_full_pages(primary: FullPage, extra: FullPage) -> FullPage:
    """Merge a follow-up capture of the same URL into the primary page object."""
    if not extra or not (extra.raw_markdown or extra.raw_html or extra.images or extra.tables_html):
        return primary
    p = primary.model_copy(deep=True)
    p.success = p.success or extra.success
    p.status = p.status or extra.status
    p.error = p.error or extra.error
    p.final_url = p.final_url or extra.final_url
    # Prefer accessible follow-up status over blocked initial status.
    if p.access_status != "accessible" and extra.access_status == "accessible":
        p.access_status = extra.access_status
        p.access_issue_type = extra.access_issue_type
        p.access_issue_reason = extra.access_issue_reason
        p.geo_restricted = extra.geo_restricted
        p.proxy_used = extra.proxy_used
        p.proxy_source = extra.proxy_source
    elif p.access_status == "unknown":
        p.access_status = extra.access_status
        p.access_issue_type = extra.access_issue_type
        p.access_issue_reason = extra.access_issue_reason
        p.geo_restricted = extra.geo_restricted
        p.proxy_used = extra.proxy_used
        p.proxy_source = extra.proxy_source
    p.access_attempts.extend(extra.access_attempts or [])
    p.title = p.title or extra.title
    p.description = p.description or extra.description
    p.canonical_url = p.canonical_url or extra.canonical_url
    p.og.update(extra.og)
    p.product_meta.update(extra.product_meta)
    p.profiles_merged = list(dict.fromkeys([*p.profiles_merged, *extra.profiles_merged, extra.fetch_profile]))

    p.raw_markdown = _coerce_text(p.raw_markdown)
    extra_md = _coerce_text(extra.raw_markdown)
    p.raw_html = _coerce_text(p.raw_html)
    extra_html = _coerce_text(extra.raw_html)

    if extra_md and extra_md not in p.raw_markdown:
        p.raw_markdown = (p.raw_markdown.rstrip() + "\n\n---\n\n" + extra_md.strip()).strip()
    if extra_html and extra_html not in p.raw_html:
        p.raw_html = (p.raw_html.rstrip() + "\n<!-- PCA_PROFILE_MERGE -->\n" + extra_html.strip()).strip()

    seen_json = {repr(x) for x in p.json_ld}
    for block in extra.json_ld:
        key = repr(block)
        if key not in seen_json:
            seen_json.add(key)
            p.json_ld.append(block)

    seen_img = {src for src, _ in p.images}
    for src, alt in extra.images:
        if src not in seen_img:
            seen_img.add(src)
            p.images.append((src, alt))

    seen_tables = {t for t in p.tables_html}
    for html in extra.tables_html:
        if html not in seen_tables:
            seen_tables.add(html)
            p.tables_html.append(html)
    return p


class _TableMd(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.caption = ""
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._in_caption = False
        self._cap_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"}:
            self._cell = []
        elif tag == "caption":
            self._in_caption = True
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
        elif tag in {"td", "th"} and self._cell is not None and self._row is not None:
            text = re.sub(r"\s+", " ", "".join(self._cell)).strip()
            self._row.append(text)
            self._cell = None
        elif tag == "caption":
            self.caption = re.sub(r"\s+", " ", "".join(self._cap_buf)).strip()
            self._cap_buf = []
            self._in_caption = False

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)
        elif self._in_caption:
            self._cap_buf.append(data)


def table_html_to_markdown(html: str) -> tuple[str, str, int, int]:
    parser = _TableMd()
    try:
        parser.feed(html)
    except Exception:
        return "", "", 0, 0
    rows = [row for row in parser.rows if any(cell.strip() for cell in row)]
    if not rows:
        return "", parser.caption, 0, 0
    cols = max(len(row) for row in rows)
    normalized = [row + [""] * (cols - len(row)) for row in rows]

    def _esc(value: str) -> str:
        return value.replace("|", "\\|")

    header = normalized[0]
    lines = ["| " + " | ".join(_esc(cell) for cell in header) + " |"]
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for row in normalized[1:]:
        lines.append("| " + " | ".join(_esc(cell) for cell in row) + " |")
    return "\n".join(lines), parser.caption, len(normalized), cols


__all__ = ["FullPage", "fetch_full", "merge_full_pages", "table_html_to_markdown"]
