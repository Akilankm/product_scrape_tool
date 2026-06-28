"""Central loguru logging setup."""

from __future__ import annotations

import sys
from loguru import logger

_DEFAULT_FORMAT = (
    "<green>{time:HH:mm:ss}</green> "
    "<level>{level:<7}</level> "
    "<cyan>{name}</cyan> | {message}"
)


def setup_logging(level: str = "INFO", *, fmt: str = _DEFAULT_FORMAT) -> None:
    """Configure loguru and force UTF-8 where supported."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    logger.remove()
    logger.add(sys.stderr, level=level, format=fmt)


__all__ = ["logger", "setup_logging"]
