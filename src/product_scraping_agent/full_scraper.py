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
from urllib.parse import urljoin, urlparse
from typing import Any

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
    ("bot_challenge", re.compile(r"\b(captcha|cloudflare|verify you are human|checking your browser|robot check|bot protection|automated access|not a robot|validatecaptcha|enter the characters you see below|enable javascript and cookies)\b", re.I)),
    ("rate_limited", re.compile(r"\b(too many requests|rate limit|temporarily blocked)\b", re.I)),
)


_GENERIC_TITLES = {"", "amazon.com", "amazon", "access denied", "robot check", "captcha", "verifica tu identidad"}
_PRODUCT_TERMS = (
    "product", "brand", "manufacturer", "ean", "gtin", "sku", "mpn", "asin",
    "price", "availability", "description", "details", "features", "specification",
    "item model", "model number", "age", "material", "dimensions", "pieces",
    "toy", "doll", "figure", "set", "package", "contents", "warning",
)
_BLOCK_TERMS = (
    "captcha", "robot check", "not a robot", "automated access", "enable javascript and cookies",
    "enter the characters", "validatecaptcha", "access denied", "request blocked", "verifica tu identidad",
    "verify your identity", "checking your browser", "unusual traffic",
)

# In-process domain profile memory. This keeps batch runs faster and more stable
# without introducing any external service or persistent state. If a profile
# succeeds for amazon.com or kuvertshop.net, the next URL from that domain tries
# the successful profile first.
_DOMAIN_PROFILE_MEMORY: dict[str, str] = {}


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^A-Za-z0-9]+", (text or "").lower()) if len(t) >= 3]


def _hint_match_score(text: str, product_hint: str = "", ean: str = "") -> int:
    text_l = (text or "").lower()
    score = 0
    if ean and ean in text_l:
        score += 20
    hint_tokens = [t for t in _tokens(product_hint) if not t.isdigit()]
    if hint_tokens:
        unique = list(dict.fromkeys(hint_tokens[:24]))
        hits = sum(1 for t in unique if t in text_l)
        score += min(18, hits * 3)
    return score


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

    # Multi-profile capture diagnostics. These are populated by fetch_full() and
    # fetch_best_full() so downstream quality/batch output can distinguish
    # "artifact created" from "real product page captured".
    capture_score: int = 0
    capture_grade: str = "not_evaluated"
    weak_capture_reasons: list[str] = Field(default_factory=list)
    real_scrape_evidence: bool = False
    capture_decision: str = "not_evaluated"
    capture_profile_used: str = ""
    capture_profiles_attempted: list[str] = Field(default_factory=list)
    capture_profile_scores: list[dict[str, Any]] = Field(default_factory=list)


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
    """JavaScript snippets for same-URL capture profiles.

    These snippets intentionally do not navigate away from the URL. They only
    scroll, expand visible sections, and stimulate lazy galleries so Crawl4AI can
    capture the product content already present behind dynamic UI.
    """
    if profile in {"standard", "load_wait", "shadow_iframe", "retry_relaxed"}:
        return None
    if profile == "full_page_scroll":
        return r"""
(async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const h = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
  for (let y = 0; y <= h; y += Math.max(450, Math.floor(window.innerHeight * 0.75))) {
    window.scrollTo(0, y); await sleep(180);
  }
  window.scrollTo(0, 0); await sleep(500);
})();
"""
    if profile == "extract_gallery_sources":
        return r"""
(async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const selectors = [
    'img', '[data-src]', '[data-srcset]', '[data-zoom-image]', '[data-large]', '[data-full]',
    '[data-image]', '[data-product-image]', '[data-a-dynamic-image]', '[data-old-hires]',
    '.thumb, .thumbnail, [class*=thumb], [class*=gallery], [class*=carousel]'
  ];
  const nodes = Array.from(document.querySelectorAll(selectors.join(','))).slice(0, 160);
  for (const el of nodes) {
    try { el.scrollIntoView({block:'center', inline:'center'}); await sleep(80); } catch(e) {}
    try { el.dispatchEvent(new MouseEvent('mouseover', {bubbles:true})); } catch(e) {}
    try { el.dispatchEvent(new MouseEvent('mouseenter', {bubbles:true})); } catch(e) {}
    try { if ((el.tagName || '').toLowerCase() !== 'a') el.click(); } catch(e) {}
    await sleep(120);
  }
  window.scrollTo(0, 0); await sleep(500);
})();
"""
    if profile == "expand_common_sections":
        return r"""
(async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const textRe = /(show more|read more|more info|details|description|specification|specifications|parameters|technical|product information|features|manufacturer|safety|see more|expand|weiter|mehr|více|parametry|popis|specifikace|detaily|ver más|más información|características|descripción|ficha técnica)/i;
  const nodes = Array.from(document.querySelectorAll('button,a,[role="button"],[aria-expanded="false"],summary,label'));
  let clicked = 0;
  for (const el of nodes) {
    const label = `${el.innerText || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''}`.trim();
    const rect = el.getBoundingClientRect();
    const visible = rect.width > 0 && rect.height > 0;
    if (visible && textRe.test(label) && clicked < 32) {
      try { el.scrollIntoView({block:'center'}); await sleep(120); el.click(); clicked++; await sleep(280); } catch(e) {}
    }
  }
  for (let y = 0; y <= document.body.scrollHeight; y += Math.max(400, Math.floor(window.innerHeight * 0.8))) {
    window.scrollTo(0, y); await sleep(140);
  }
  window.scrollTo(0, 0); await sleep(500);
})();
"""
    return None


def _profile_sequence(cfg: Config) -> list[str]:
    raw = cfg.scrape_profile_sequence or "standard"
    profiles = [p.strip().lower() for p in re.split(r"[,;\s]+", raw) if p.strip()]
    allowed = [
        "standard", "load_wait", "full_page_scroll", "expand_common_sections",
        "extract_gallery_sources", "shadow_iframe", "retry_relaxed",
    ]
    out: list[str] = []
    for p in profiles:
        if p in allowed and p not in out:
            out.append(p)
    if "standard" not in out:
        out.insert(0, "standard")
    return out[: max(1, cfg.scrape_profile_max_profiles)]


def score_full_page_capture(page: FullPage, *, product_hint: str = "", ean: str = "") -> dict[str, Any]:
    """Score whether a Crawl4AI profile captured real product evidence.

    A 200 response, a single image candidate, or caller input is not enough to
    mark a page as a real product capture. Rich content with incidental block
    terms is treated as mixed/needs-review rather than hard-failed.
    """
    md = _coerce_text(page.raw_markdown)
    html = _coerce_text(page.raw_html)
    title = (page.title or "").strip()
    text = "\n".join([title, page.description or "", md, html[:25_000]]).lower()
    md_chars = len(md)
    html_chars = len(html)
    payload_chars = md_chars + html_chars
    product_signal_count = sum(1 for term in _PRODUCT_TERMS if term in text)
    block_signal_count = sum(1 for term in _BLOCK_TERMS if term in text)
    generic_title = title.lower() in _GENERIC_TITLES or not title
    structured_count = len(page.json_ld or []) + len(page.tables_html or []) + len(page.og or {}) + len(page.product_meta or {})
    image_count = len(page.images or [])
    has_payload_text = payload_chars > 0

    score = 0
    reasons: list[str] = []
    positives: list[str] = []

    if not has_payload_text and structured_count == 0:
        reasons.extend(["no_readable_content", "empty_text_payload", "very_low_markdown", "very_low_html"])
        if image_count:
            reasons.append("image_candidates_without_text_payload")
        if page.access_status != "accessible":
            reasons.append(f"access_status={page.access_status}")
        return {
            "profile": page.fetch_profile,
            "score": 0,
            "grade": "blocked_or_shell",
            "capture_decision": "input_url_only_artifact",
            "real_scrape_evidence": False,
            "weak_capture": True,
            "weak_reasons": list(dict.fromkeys(reasons)),
            "positive_signals": positives,
            "markdown_chars": md_chars,
            "html_chars": html_chars,
            "title": title,
            "product_signal_count": product_signal_count,
            "block_signal_count": block_signal_count,
            "structured_signal_count": structured_count,
            "image_candidate_count": image_count,
            "access_status": page.access_status,
            "status": page.status,
            "success": page.success,
        }

    if page.access_status == "accessible" and page.success:
        score += 12; positives.append("accessible_success")
    elif page.access_status == "accessible":
        score += 5; positives.append("accessible_payload")
    else:
        score -= 25; reasons.append(f"access_status={page.access_status}")

    if md_chars >= 12_000:
        score += 30; positives.append("rich_markdown")
    elif md_chars >= 6_000:
        score += 22; positives.append("good_markdown")
    elif md_chars >= 2_500:
        score += 13; positives.append("moderate_markdown")
    elif md_chars >= 900:
        score += 6; positives.append("thin_markdown")
    else:
        reasons.append("very_low_markdown")

    if html_chars >= 80_000:
        score += 12; positives.append("rich_html")
    elif html_chars >= 25_000:
        score += 8; positives.append("good_html")
    elif html_chars < 5_000 and md_chars < 2_500:
        score -= 10; reasons.append("very_low_html")

    if not generic_title:
        score += 8; positives.append("specific_title")
    else:
        score -= 7; reasons.append("generic_or_missing_title")

    if page.json_ld:
        score += min(28, 18 + len(page.json_ld) * 5); positives.append("json_ld")
    if page.tables_html:
        score += min(18, 8 + len(page.tables_html) * 4); positives.append("tables")
    if page.og or page.product_meta:
        score += min(12, 4 + len(page.og) + len(page.product_meta)); positives.append("meta_tags")

    if has_payload_text or structured_count:
        if image_count >= 12:
            score += 12; positives.append("many_image_candidates")
        elif image_count >= 4:
            score += 7; positives.append("image_candidates")
        elif image_count == 0:
            reasons.append("no_image_candidates")
    elif image_count:
        reasons.append("image_candidates_without_text_payload")

    if product_signal_count >= 8:
        score += 15; positives.append("many_product_terms")
    elif product_signal_count >= 3:
        score += 8; positives.append("some_product_terms")
    elif product_signal_count < 2:
        reasons.append("few_product_signals")

    hint_score = _hint_match_score(text, product_hint, ean)
    if hint_score:
        score += hint_score; positives.append("input_identity_match")
    elif product_hint or ean:
        reasons.append("no_input_identity_match")

    severe_block = bool(block_signal_count and (
        page.access_status != "accessible" or (md_chars < 2_500 and html_chars < 8_000)
    ))
    mild_block = bool(block_signal_count and not severe_block)
    if severe_block:
        score -= min(70, 35 + block_signal_count * 10); reasons.append("block_or_challenge_terms")
    elif mild_block:
        score -= min(18, 6 + block_signal_count * 4); reasons.append("block_terms_present_in_rich_capture")

    if md_chars < 1200 and html_chars < 8000 and structured_count == 0:
        score -= 18; reasons.append("thin_shell_no_structured_evidence")
    if generic_title and md_chars < 2500:
        score -= 12; reasons.append("generic_title_with_low_text")

    real_evidence = bool(
        page.json_ld or page.tables_html or page.product_meta
        or (md_chars >= 2_500 and product_signal_count >= 3)
        or (md_chars >= 6_000 and not generic_title)
        or (html_chars >= 80_000 and image_count >= 4 and product_signal_count >= 2)
    )
    if severe_block:
        real_evidence = False

    score = max(0, min(100, score))
    if not real_evidence:
        grade = "blocked_or_shell" if score < 35 else "weak"
        decision = "blocked_shell_capture" if page.access_status != "accessible" or severe_block else "weak_no_real_product_capture"
    elif mild_block:
        grade = "mixed_capture"
        decision = "mixed_capture_needs_review"
    elif score >= 78:
        grade = "strong"
        decision = "rich_product_capture"
    elif score >= 58:
        grade = "usable"
        decision = "usable_product_capture"
    elif score >= 35:
        grade = "weak"
        decision = "weak_product_capture"
    else:
        grade = "blocked_or_shell"
        decision = "blocked_shell_capture"
        real_evidence = False

    return {
        "profile": page.fetch_profile,
        "score": score,
        "grade": grade,
        "capture_decision": decision,
        "real_scrape_evidence": bool(real_evidence),
        "weak_capture": grade in {"weak", "blocked_or_shell", "mixed_capture"} or decision != "rich_product_capture",
        "weak_reasons": list(dict.fromkeys(reasons)),
        "positive_signals": list(dict.fromkeys(positives)),
        "markdown_chars": md_chars,
        "html_chars": html_chars,
        "title": title,
        "product_signal_count": product_signal_count,
        "block_signal_count": block_signal_count,
        "structured_signal_count": structured_count,
        "image_candidate_count": image_count,
        "access_status": page.access_status,
        "status": page.status,
        "success": page.success,
    }

def _apply_capture_score(page: FullPage, *, product_hint: str = "", ean: str = "") -> FullPage:
    diag = score_full_page_capture(page, product_hint=product_hint, ean=ean)
    page.capture_score = int(diag["score"])
    page.capture_grade = str(diag["grade"])
    page.weak_capture_reasons = list(diag.get("weak_reasons") or [])
    page.real_scrape_evidence = bool(diag.get("real_scrape_evidence"))
    page.capture_decision = str(diag.get("capture_decision") or "not_evaluated")
    page.capture_profile_used = page.fetch_profile
    if not page.capture_profiles_attempted:
        page.capture_profiles_attempted = [page.fetch_profile]
    if not page.capture_profile_scores:
        page.capture_profile_scores = [diag]
    return page


def _merge_auxiliary_signals(primary: FullPage, extra: FullPage) -> FullPage:
    """Merge non-noisy structured/image/table signals from another profile.

    We avoid appending weak/block-page text into the selected capture, but keep
    useful images/tables/metadata discovered by gallery/section profiles.
    """
    if not extra or extra is primary:
        return primary
    p = primary.model_copy(deep=True)
    p.access_attempts.extend(extra.access_attempts or [])
    p.capture_profiles_attempted = list(dict.fromkeys([*p.capture_profiles_attempted, extra.fetch_profile, *extra.capture_profiles_attempted]))
    existing_scores = {d.get("profile") for d in p.capture_profile_scores if isinstance(d, dict)}
    for d in extra.capture_profile_scores or [score_full_page_capture(extra)]:
        if isinstance(d, dict) and d.get("profile") not in existing_scores:
            p.capture_profile_scores.append(d)
            existing_scores.add(d.get("profile"))
    p.profiles_merged = list(dict.fromkeys([*p.profiles_merged, *extra.profiles_merged, extra.fetch_profile]))
    if not p.title and extra.title:
        p.title = extra.title
    if not p.description and extra.description:
        p.description = extra.description
    if not p.canonical_url and extra.canonical_url:
        p.canonical_url = extra.canonical_url
    p.og.update(extra.og or {})
    p.product_meta.update(extra.product_meta or {})
    seen_json = {repr(x) for x in p.json_ld}
    for block in extra.json_ld or []:
        key = repr(block)
        if key not in seen_json:
            seen_json.add(key); p.json_ld.append(block)
    seen_img = {src for src, _ in p.images}
    for src, alt in extra.images or []:
        if src not in seen_img:
            seen_img.add(src); p.images.append((src, alt))
    seen_tables = {t for t in p.tables_html}
    for html in extra.tables_html or []:
        if html not in seen_tables:
            seen_tables.add(html); p.tables_html.append(html)
    return p


def _domain_key(url: str) -> str:
    host = (urlparse(url or "").netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _profile_sequence_for_url(cfg: Config, url: str) -> list[str]:
    profiles = _profile_sequence(cfg)
    host = _domain_key(url)
    preferred = _DOMAIN_PROFILE_MEMORY.get(host)
    if preferred and preferred in profiles:
        return [preferred, *[p for p in profiles if p != preferred]]
    return profiles


def _remember_domain_profile(url: str, page: FullPage) -> None:
    host = _domain_key(url)
    if not host:
        return
    if page.capture_decision in {"rich_product_capture", "usable_product_capture"} or (page.real_scrape_evidence and page.capture_score >= 58):
        _DOMAIN_PROFILE_MEMORY[host] = page.capture_profile_used or page.fetch_profile


async def fetch_best_full(
    cfg: Config,
    url: str,
    *,
    country_code: str = "",
    product_hint: str = "",
    ean: str = "",
) -> FullPage:
    """Run multiple Crawl4AI profiles and return the richest same-URL capture."""
    profiles = _profile_sequence_for_url(cfg, url)
    best: FullPage | None = None
    captures: list[FullPage] = []
    logger.info("full_scraper: multi-profile sequence={}", ", ".join(profiles))
    for profile in profiles:
        page = await fetch_full(cfg, url, profile=profile, country_code=country_code, product_hint=product_hint, ean=ean)
        captures.append(page)
        logger.info(
            "full_scraper[{}]: capture_score={} grade={} decision={} real={} weak_reasons={}",
            profile, page.capture_score, page.capture_grade, page.capture_decision, page.real_scrape_evidence, page.weak_capture_reasons,
        )
        if best is None or (page.capture_score, len(page.raw_markdown or ""), len(page.images or [])) > (best.capture_score, len(best.raw_markdown or ""), len(best.images or [])):
            best = page
        if page.capture_score >= cfg.scrape_profile_early_stop_score and page.real_scrape_evidence:
            logger.info("full_scraper: early stop at profile={} score={}", profile, page.capture_score)
            break
    assert best is not None
    selected = best.model_copy(deep=True)
    selected.capture_profile_used = best.fetch_profile
    selected.capture_profiles_attempted = [c.fetch_profile for c in captures]
    selected.capture_profile_scores = [score_full_page_capture(c, product_hint=product_hint, ean=ean) for c in captures]
    # Add non-text signals from other captures without contaminating selected text.
    for cap in captures:
        if cap.fetch_profile != selected.fetch_profile and cap.capture_score >= 25:
            selected = _merge_auxiliary_signals(selected, cap)
    # Re-score after auxiliary merge.
    _apply_capture_score(selected, product_hint=product_hint, ean=ean)
    selected.capture_profile_used = best.fetch_profile
    selected.capture_profiles_attempted = [c.fetch_profile for c in captures]
    selected.capture_profile_scores = [score_full_page_capture(c, product_hint=product_hint, ean=ean) for c in captures]
    _remember_domain_profile(url, selected)
    logger.info(
        "full_scraper: selected profile={} score={} grade={} decision={} real={} attempted={}",
        selected.capture_profile_used, selected.capture_score, selected.capture_grade, selected.capture_decision, selected.real_scrape_evidence, ", ".join(selected.capture_profiles_attempted),
    )
    return selected



async def fetch_full(cfg: Config, url: str, *, profile: str = "standard", country_code: str = "", product_hint: str = "", ean: str = "") -> FullPage:
    """Render a product URL with Crawl4AI and extract text/html/images/tables."""
    try:
        from crawl4ai import CacheMode
    except ImportError as exc:
        return FullPage(url=url, fetch_profile=profile, error=f"crawl4ai import failed: {exc}")

    base_timeout_ms = int(cfg.scrape_timeout * 1000)
    env_scan_full = os.getenv("PCA_SCAN_FULL_PAGE", "0").lower() in {"1", "true", "yes"}
    scan_full = env_scan_full or profile in {"full_page_scroll", "expand_common_sections", "extract_gallery_sources", "shadow_iframe"}

    def _mk_run_cfg(timeout_ms: int, *, last_ditch: bool = False, proxy_url: str = ""):
        kwargs = {
            "cache_mode": CacheMode.BYPASS,
            "page_timeout": timeout_ms,
            "wait_until": "domcontentloaded" if last_ditch else ("load" if profile in {"load_wait", "shadow_iframe"} else cfg.scrape_wait_until),
            "delay_before_return_html": (4.0 if profile in {"load_wait", "shadow_iframe"} else (3.0 if profile != "standard" else (1.5 if last_ditch else 0.5))),
            "word_count_threshold": 5,
            "exclude_external_links": False,
            "remove_overlay_elements": not last_ditch,
            "verbose": False,
            "scan_full_page": False if last_ditch else scan_full,
            "screenshot": False,
            "process_iframes": profile == "shadow_iframe",
            "flatten_shadow_dom": profile == "shadow_iframe",
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

    _apply_capture_score(out, product_hint=product_hint, ean=ean)
    logger.info(
        "full_scraper[{}]: status={} access={} proxy={} html={}KB md={}KB images={} tables={} json_ld={} score={} grade={}",
        profile,
        out.status,
        out.access_status,
        out.proxy_source or "direct",
        len(out.raw_html) // 1024,
        len(out.raw_markdown) // 1024,
        len(out.images),
        len(out.tables_html),
        len(out.json_ld),
        out.capture_score,
        out.capture_grade,
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
    p.capture_profiles_attempted = list(dict.fromkeys([*p.capture_profiles_attempted, extra.fetch_profile, *extra.capture_profiles_attempted]))
    existing_score_profiles = {d.get("profile") for d in p.capture_profile_scores if isinstance(d, dict)}
    for d in extra.capture_profile_scores or [score_full_page_capture(extra)]:
        if isinstance(d, dict) and d.get("profile") not in existing_score_profiles:
            p.capture_profile_scores.append(d)
            existing_score_profiles.add(d.get("profile"))

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
    # Preserve the best score if already computed; otherwise produce a generic diagnostic.
    if not p.capture_score:
        _apply_capture_score(p)
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


__all__ = ["FullPage", "fetch_full", "fetch_best_full", "merge_full_pages", "table_html_to_markdown", "score_full_page_capture"]
