"""Small text helpers used by artifact creation."""

from __future__ import annotations

from . import patterns as _P


def truncate_text(text: str | None, n: int) -> str:
    text = coerce_text_payload(text)
    if not text:
        return ""
    if len(text) <= n:
        return text
    return text[:n] + f"\n…[truncated, {len(text) - n} more chars]"


def clean_markdown(md: str | None) -> str:
    md = coerce_text_payload(md)
    if not md:
        return ""
    out = _P.MD_IMAGE.sub("", md)
    out = _P.MD_LINK.sub(r"\1", out)
    out = _P.EXCESS_NEWLINES.sub("\n\n", out)
    out = _P.EXCESS_SPACES.sub(" ", out)
    return out.strip()


def coerce_text_payload(value, *, max_chars: int | None = None, _depth: int = 0) -> str:
    """Normalize Crawl4AI/Pydantic text payload variants into plain strings.

    Crawl4AI may return markdown/html fields as strings in one version and as
    MarkdownGenerationResult-like objects in another. Artifact code must never
    call ``len()`` or write files directly on those objects.
    """
    if value is None or _depth > 6:
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
            "body",
        ):
            if key in value:
                text = coerce_text_payload(value.get(key), max_chars=max_chars, _depth=_depth + 1)
                if text:
                    break
    elif isinstance(value, (list, tuple, set)):
        parts = [coerce_text_payload(v, _depth=_depth + 1) for v in value]
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
            "body",
        ):
            try:
                nested = getattr(value, attr)
            except Exception:
                continue
            if nested is value:
                continue
            text = coerce_text_payload(nested, max_chars=max_chars, _depth=_depth + 1)
            if text:
                break
        if not text:
            try:
                dumped = value.model_dump()
            except Exception:
                dumped = None
            if isinstance(dumped, dict):
                text = coerce_text_payload(dumped, max_chars=max_chars, _depth=_depth + 1)
        if not text:
            rendered = str(value)
            if "MarkdownGenerationResult" not in rendered and not rendered.startswith("<"):
                text = rendered
    text = text or ""
    return text[:max_chars] if max_chars and len(text) > max_chars else text


def safe_text_len(value) -> int:
    """Length of a text-like payload after Crawl4AI compatibility coercion."""
    return len(coerce_text_payload(value))


def has_text_payload(value) -> bool:
    return bool(coerce_text_payload(value).strip())


def digits_only(s: str | None) -> str:
    return _P.NON_DIGITS.sub("", s or "")


__all__ = ["truncate_text", "clean_markdown", "digits_only", "coerce_text_payload", "safe_text_len", "has_text_payload"]
