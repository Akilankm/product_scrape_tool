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

from .config import Config
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


class FullPage(BaseModel):
    """Rendered page payload and extracted browser-level signals."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    url: str
    final_url: str = ""
    fetch_profile: str = "standard"
    success: bool = False
    status: int = 0
    error: str = ""
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


async def fetch_full(cfg: Config, url: str, *, profile: str = "standard") -> FullPage:
    """Render a product URL with Crawl4AI and extract text/html/images/tables."""
    try:
        from crawl4ai import CacheMode
    except ImportError as exc:
        return FullPage(url=url, fetch_profile=profile, error=f"crawl4ai import failed: {exc}")

    base_timeout_ms = int(cfg.scrape_timeout * 1000)
    env_scan_full = os.getenv("PCA_SCAN_FULL_PAGE", "0").lower() in {"1", "true", "yes"}
    scan_full = env_scan_full or profile in {"full_page_scroll", "expand_common_sections", "extract_gallery_sources"}

    def _mk_run_cfg(timeout_ms: int, *, last_ditch: bool = False):
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
        js = _profile_js(profile)
        if js and not last_ditch:
            kwargs["js_code"] = js
        return _crawler_config_kwargs(kwargs)

    async def _attempt(timeout_ms: int, *, last_ditch: bool = False):
        try:
            return await _crawler_arun_many(
                cfg,
                [url],
                _mk_run_cfg(timeout_ms, last_ditch=last_ditch),
                timeout=(timeout_ms / 1000.0) * 1.8,
            )
        except asyncio.TimeoutError:
            logger.warning("full_scraper[{}]: hard timeout for {}", profile, url)
            return []
        except Exception as exc:
            logger.warning("full_scraper[{}]: crawl failed for {} — {}", profile, url, exc)
            return []

    results = await _attempt(base_timeout_ms if profile == "standard" else int(base_timeout_ms * 1.6))
    result = results[0] if results else None

    def _transient(r) -> bool:
        if r is None:
            return True
        status = int(getattr(r, "status_code", 0) or 0)
        if status in {401, 403, 408, 429, 451} or 500 <= status < 600:
            return True
        if not bool(getattr(r, "success", False)):
            md_obj = getattr(r, "markdown", None)
            md_text = md_obj if isinstance(md_obj, str) else (getattr(md_obj, "raw_markdown", "") if md_obj else "")
            if not (getattr(r, "cleaned_html", "") or getattr(r, "html", "") or md_text):
                return True
        return False

    if _transient(result):
        logger.warning("full_scraper[{}]: retrying transient fetch for {}", profile, url)
        results = await _attempt(base_timeout_ms * 2)
        result = results[0] if results else result

    if _transient(result) or profile == "retry_relaxed":
        logger.warning("full_scraper[{}]: last-ditch DOM grab for {}", profile, url)
        results = await _attempt(15_000, last_ditch=True)
        if results:
            result = results[0]

    out = FullPage(url=url, fetch_profile=profile, profiles_merged=[profile])
    if result is None:
        out.error = "no crawl result"
        return out

    out.success = bool(getattr(result, "success", False))
    out.status = int(getattr(result, "status_code", 0) or 0)
    out.error = str(getattr(result, "error_message", "") or "")
    out.final_url = getattr(result, "url", "") or getattr(result, "redirected_url", "") or url

    md_obj = getattr(result, "markdown", None)
    if isinstance(md_obj, str):
        out.raw_markdown = md_obj
    elif md_obj is not None:
        out.raw_markdown = getattr(md_obj, "raw_markdown", "") or getattr(md_obj, "fit_markdown", "") or ""

    out.raw_html = getattr(result, "cleaned_html", "") or getattr(result, "html", "") or ""

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
        "full_scraper[{}]: status={} html={}KB md={}KB images={} tables={} json_ld={}",
        profile,
        out.status,
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
    p.title = p.title or extra.title
    p.description = p.description or extra.description
    p.canonical_url = p.canonical_url or extra.canonical_url
    p.og.update(extra.og)
    p.product_meta.update(extra.product_meta)
    p.profiles_merged = list(dict.fromkeys([*p.profiles_merged, *extra.profiles_merged, extra.fetch_profile]))

    if extra.raw_markdown and extra.raw_markdown not in p.raw_markdown:
        p.raw_markdown = (p.raw_markdown.rstrip() + "\n\n---\n\n" + extra.raw_markdown.strip()).strip()
    if extra.raw_html and extra.raw_html not in p.raw_html:
        p.raw_html = (p.raw_html.rstrip() + "\n<!-- PCA_PROFILE_MERGE -->\n" + extra.raw_html.strip()).strip()

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
