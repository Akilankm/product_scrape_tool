"""Shared HTTP and image-encoding utilities."""

from __future__ import annotations

import base64
import io
import ssl

import httpx
try:
    import truststore
except ImportError:  # dependency is declared; fallback keeps imports testable before pdm install
    truststore = None
from PIL import Image

SSL_CTX = (
    truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if truststore is not None
    else ssl.create_default_context()
)

BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0 Safari/537.36"
    ),
    "Accept": "image/jpeg,image/png,image/webp,image/gif,*/*;q=0.5",
}

IMAGE_MIME_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
    "image/avif": ".avif",
    "image/tiff": ".tif",
}

VISION_MAX_EDGE: int = 384
VISION_JPEG_QUALITY: int = 70


def make_async_client(*, timeout: float = 15.0, headers: dict[str, str] | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, verify=SSL_CTX, headers=headers or {})


def resize_image_to_jpeg(
    data: bytes,
    *,
    max_edge: int = VISION_MAX_EDGE,
    quality: int = VISION_JPEG_QUALITY,
) -> bytes | None:
    """Re-encode image bytes into a small RGB JPEG for vision payloads."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            w, h = im.size
            scale = min(max_edge / max(w, h), 1.0)
            if scale < 1.0:
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=quality, optimize=True)
            return buf.getvalue()
    except Exception:
        return None


def to_data_url_jpeg(jpeg_bytes: bytes) -> str:
    b64 = base64.b64encode(jpeg_bytes).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


__all__ = [
    "SSL_CTX",
    "BROWSER_HEADERS",
    "IMAGE_MIME_EXT",
    "make_async_client",
    "resize_image_to_jpeg",
    "to_data_url_jpeg",
]
