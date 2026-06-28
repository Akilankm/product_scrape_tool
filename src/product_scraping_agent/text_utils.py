"""Small text helpers used by artifact creation."""

from __future__ import annotations

from . import patterns as _P


def truncate_text(text: str | None, n: int) -> str:
    if not text:
        return ""
    if len(text) <= n:
        return text
    return text[:n] + f"\n…[truncated, {len(text) - n} more chars]"


def clean_markdown(md: str | None) -> str:
    if not md:
        return ""
    out = _P.MD_IMAGE.sub("", md)
    out = _P.MD_LINK.sub(r"\1", out)
    out = _P.EXCESS_NEWLINES.sub("\n\n", out)
    out = _P.EXCESS_SPACES.sub(" ", out)
    return out.strip()


def digits_only(s: str | None) -> str:
    return _P.NON_DIGITS.sub("", s or "")


__all__ = ["truncate_text", "clean_markdown", "digits_only"]
