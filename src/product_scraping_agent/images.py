"""Image discovery, downloading, and per-image LLM vision description.

Behaviour:

* Downloads candidate images into ``<out_dir>/images/`` only after validating
  that the response is a real raster image; final cleanup keeps only
  vision-confirmed product images in that folder.
* Builds a small JPEG thumbnail of each image and sends it to GPT-4o as
  base64 data-URL **bytes** (never as a text URL — image URLs are not
  transmitted to the model).
* Per-image vision prompt is kept under ~480 chars to satisfy the corporate
  AOAI gateway's multimodal text-length cap.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import re
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import httpx
from PIL import Image

from . import patterns as _P
from .log import logger
from .prompts import P
from .services.http import (
    BROWSER_HEADERS,
    IMAGE_MIME_EXT,
    SSL_CTX,
    resize_image_to_jpeg,
    to_data_url_jpeg,
)
from .models import ImageRef


# Cap: don't download huge originals (banner videos, posters etc.).
_MAX_BYTES = 8_000_000
_FINAL_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_IMAGE_RESPONSE_MIME_PREFIX = "image/"



def _compute_phash(im: "Image.Image") -> str:
    """Compute a 64-bit DCT perceptual hash for the given PIL image.

    Returns a 16-char lowercase hex string, or ``""`` on any failure.
    pHash survives JPEG re-encoding, minor crop, and rescaling — making
    it the right tool for "is this the same product photo published by
    a different host?" The bit pattern is the standard pHash recipe:
    32×32 greyscale → 2-D DCT → top-left 8×8 block → median-threshold.
    """
    try:
        import numpy as np
    except Exception:  # pragma: no cover  # noqa: BLE001
        return ""
    try:
        small = im.convert("L").resize((32, 32), Image.LANCZOS)
        arr = np.asarray(small, dtype=float)
        # 2-D DCT-II via separable 1-D transforms (avoids scipy import).
        # The classic pHash recipe.
        n = 32
        k = np.arange(n)
        basis = np.cos(np.pi * (2 * k[:, None] + 1) * k[None, :] / (2 * n))
        dct1 = basis @ arr
        dct2 = dct1 @ basis.T
        block = dct2[:8, :8].flatten()
        # Exclude the DC component when picking the threshold so a
        # uniformly bright/dark image doesn't yield an all-zero hash.
        median = float(np.median(block[1:]))
        bits = (block > median).astype(int)
        bits[0] = 0
        val = 0
        for b in bits:
            val = (val << 1) | int(b)
        return f"{val:016x}"
    except Exception:  # noqa: BLE001
        return ""


def _should_skip(url: str, alt: str) -> bool:
    if url.startswith("data:"):
        return True
    if _P.IMAGE_SKIP_DOWNLOAD.search(url):
        return True
    return False


# ----------------------------------------------------------------------
# Download
# ----------------------------------------------------------------------
def _origin_from_url(url: str) -> str:
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""
    except Exception:
        return ""


def _strip_query(url: str) -> str:
    try:
        p = urlparse(url)
        return urlunparse((p.scheme, p.netloc, p.path, p.params, "", ""))
    except Exception:
        return url


def _image_headers(*, referer: str, image_url: str, strategy: str) -> dict[str, str]:
    """Build progressively stronger CDN-friendly image request headers."""
    headers = dict(BROWSER_HEADERS)
    headers.update({
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Referer": referer or _origin_from_url(image_url),
        "Sec-Fetch-Dest": "image",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
    })
    accept_language = os.getenv("PCA_IMAGE_ACCEPT_LANGUAGE") or os.getenv("PCA_ACCEPT_LANGUAGE") or ""
    if accept_language:
        headers["Accept-Language"] = accept_language
    if strategy == "origin_referer":
        origin = _origin_from_url(referer) or _origin_from_url(image_url)
        if origin:
            headers["Referer"] = origin + "/"
            headers["Origin"] = origin
    elif strategy == "same_origin_referer":
        origin = _origin_from_url(image_url)
        if origin:
            headers["Referer"] = origin + "/"
            headers["Origin"] = origin
    elif strategy == "no_referer":
        headers.pop("Referer", None)
        headers.pop("Origin", None)
        headers["Sec-Fetch-Site"] = "none"
    return headers


async def _try_playwright_request(url: str, *, headers: dict[str, str], timeout: float) -> tuple[int, bytes, dict[str, str], str]:
    """Last-resort image fetch through Playwright's request stack.

    This does not bypass access controls; it only helps with CDNs that reject
    plain HTTP clients but accept browser-shaped requests. It is intentionally
    optional and best-effort because Crawl4AI already brings Playwright in most
    runtime environments.
    """
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # pragma: no cover - depends on optional runtime
        return 0, b"", {}, f"playwright unavailable: {type(exc).__name__}: {exc}"
    try:
        async with async_playwright() as pw:
            ctx = await pw.request.new_context(extra_http_headers=headers)
            try:
                resp = await ctx.get(url, timeout=timeout * 1000)
                body = await resp.body()
                return resp.status, body, {k.lower(): v for k, v in resp.headers.items()}, "playwright_request"
            finally:
                await ctx.dispose()
    except Exception as exc:  # noqa: BLE001
        return 0, b"", {}, f"playwright_request {type(exc).__name__}: {exc}"


async def _download_one(
    client: httpx.AsyncClient, url: str, alt: str, referer: str, idx: int,
    images_dir: Path,
) -> ImageRef:
    ref = ImageRef(url=url, alt=alt)
    timeout = float(os.getenv("PCA_IMAGE_DOWNLOAD_TIMEOUT", "20") or "20")
    retry_enabled = os.getenv("PCA_IMAGE_RETRY_STRATEGIES_ENABLED", "1").lower() in {"1", "true", "yes", "y", "on"}
    strip_query_retry = os.getenv("PCA_IMAGE_RETRY_STRIP_QUERY", "0").lower() in {"1", "true", "yes", "y", "on"}
    browser_fallback = os.getenv("PCA_IMAGE_BROWSER_REQUEST_FALLBACK", "1").lower() in {"1", "true", "yes", "y", "on"}

    attempts: list[tuple[str, str, dict[str, str]]] = [
        ("referer", url, _image_headers(referer=referer, image_url=url, strategy="referer")),
    ]
    if retry_enabled:
        attempts.extend([
            ("origin_referer", url, _image_headers(referer=referer, image_url=url, strategy="origin_referer")),
            ("same_origin_referer", url, _image_headers(referer=referer, image_url=url, strategy="same_origin_referer")),
            ("no_referer", url, _image_headers(referer=referer, image_url=url, strategy="no_referer")),
        ])
        if strip_query_retry and "?" in url:
            stripped = _strip_query(url)
            if stripped and stripped != url:
                attempts.append(("strip_query", stripped, _image_headers(referer=referer, image_url=stripped, strategy="referer")))

    data = b""
    headers: dict[str, str] = {}
    status_code = 0
    source = ""
    last_error = ""

    for name, candidate_url, hdrs in attempts:
        try:
            r = await client.get(candidate_url, headers=hdrs, follow_redirects=True, timeout=timeout)
            status_code = int(r.status_code)
            headers = {k.lower(): v for k, v in r.headers.items()}
            ref.download_attempts.append({
                "strategy": name,
                "url_changed": candidate_url != url,
                "status": status_code,
                "bytes": len(r.content or b""),
            })
            if r.status_code == 200 and r.content:
                data = r.content
                source = name
                break
            last_error = f"http {r.status_code}"
            if r.status_code not in {401, 403, 404, 408, 429, 500, 502, 503, 504}:
                break
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            ref.download_attempts.append({"strategy": name, "status": 0, "error": last_error[:300]})

    if not data and browser_fallback and retry_enabled and last_error.startswith("http 403"):
        hdrs = _image_headers(referer=referer, image_url=url, strategy="referer")
        status_code, data, headers, source_or_error = await _try_playwright_request(url, headers=hdrs, timeout=timeout)
        ref.download_attempts.append({
            "strategy": "playwright_request",
            "status": status_code,
            "bytes": len(data or b""),
            "error": "" if data and status_code == 200 else source_or_error[:300],
        })
        if data and status_code == 200:
            source = "playwright_request"
        elif source_or_error:
            last_error = source_or_error

    if not data:
        ref.error = last_error or f"http {status_code}" if status_code else "download failed"
        return ref
    if len(data) > _MAX_BYTES:
        ref.error = f"too large ({len(data)} bytes)"
        return ref

    mime = (headers.get("content-type", "") or "").split(";")[0].strip().lower()
    sha8 = hashlib.sha1(data).hexdigest()[:8]

    # Final images/ must never contain HTML/error payloads, binary unknowns,
    # SVG chrome, tracking pixels, or any file that Pillow cannot decode as a
    # raster image. Keep such failures only in image_manifest.json.
    if mime and not mime.startswith(_IMAGE_RESPONSE_MIME_PREFIX):
        ref.error = f"non-image response mime={mime or 'unknown'} status={status_code}"
        ref.bytes_size = len(data)
        ref.mime = mime or ""
        ref.sha8 = sha8
        return ref
    if mime == "image/svg+xml":
        ref.error = "svg/vector image excluded from final product image set"
        ref.bytes_size = len(data)
        ref.mime = mime
        ref.sha8 = sha8
        return ref

    width = height = 0
    phash_hex = ""
    ext = IMAGE_MIME_EXT.get(mime, "")
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
            fmt = (im.format or "").lower()
            if not ext and fmt:
                ext = ".jpg" if fmt in {"jpeg", "jpg"} else f".{fmt}"
            phash_hex = _compute_phash(im)
    except Exception as exc:  # noqa: BLE001
        ref.error = f"invalid image payload: {type(exc).__name__}"
        ref.bytes_size = len(data)
        ref.mime = mime or ""
        ref.sha8 = sha8
        return ref

    ext = (ext or "").lower()
    if ext == ".jpe":
        ext = ".jpg"
    if ext not in _FINAL_IMAGE_EXTS:
        # Convert/normalize uncommon but valid raster formats into PNG rather
        # than allowing .bin/.html/.avif/etc. into the clean artifact folder.
        try:
            with Image.open(io.BytesIO(data)) as im:
                buf = io.BytesIO()
                im.convert("RGB").save(buf, format="PNG")
                data = buf.getvalue()
                mime = "image/png"
                ext = ".png"
                sha8 = hashlib.sha1(data).hexdigest()[:8]
        except Exception as exc:  # noqa: BLE001
            ref.error = f"unsupported final image type ext={ext or 'unknown'} mime={mime or 'unknown'}: {type(exc).__name__}"
            ref.bytes_size = len(data)
            ref.mime = mime or ""
            ref.sha8 = sha8
            return ref

    fname = f"{idx:03d}_{sha8}{ext}"
    path = images_dir / fname
    path.write_bytes(data)

    ref.local_path = path
    ref.bytes_size = len(data)
    ref.sha8 = sha8
    ref.phash = phash_hex
    ref.mime = mime or ""
    ref.width = width
    ref.height = height
    ref.download_source = source or "http"
    return ref


def _build_vision_thumb(path: Path) -> str | None:
    """Return base64 ``data:`` URL of a small JPEG thumb. None on failure."""
    try:
        data = path.read_bytes()
    except Exception:
        return None
    jpg = resize_image_to_jpeg(data)
    if not jpg:
        return None
    return to_data_url_jpeg(jpg)




def _vision_describe_sync(b64_url: str, alt: str, product_hint: str) -> str:
    """Sync vision call (LLM SDK is sync). Run via asyncio.to_thread.

    The prompt asks the model for a leading ``RELATED: yes|no`` line
    followed by 4-7 bullets. The caller parses the verdict to decide
    whether to keep the file on disk.
    """
    # Keep prompt SHORT — gateway requires <500 chars in multimodal payload.
    hint = (product_hint or "").strip()[:80]
    alt_h = (alt or "").strip()[:60]
    prompt = (
        "Product context: " + (hint or "(none)") + ". "
        "alt=" + alt_h + ". "
        "First line MUST be 'RELATED: yes' if the image shows the same "
        "product or its packaging/accessories, else 'RELATED: no'. "
        "Then 4-7 short bullets describing what is visible."
    )
    try:
        from .services.llm import get_llm_service

        llm = get_llm_service()
        resp = llm.predict(
            prompt,
            system_prompt=P.IMAGE_VISION.system,
            image=b64_url,
            image_detail="high",
            max_tokens=320,
            purpose=P.IMAGE_VISION.name,
        )
        return (resp.content or "").strip()
    except Exception as exc:
        logger.warning("vision describe failed: {}", exc)
        return ""


def _vision_relevance_sync(b64_url: str, alt: str, product_hint: str) -> str:
    """Cheap RELATED-only verdict on a thumbnail. Returns 'yes' / 'no' / ''.

    A single-token classification used as the relevance gate. Run on
    EVERY downloaded image (not just the rich-description picks) so
    unrelated banners, logos, payment icons, recommendation tiles, and
    cross-sell carousels are deleted before they pollute ``images/``.

    Uses ``image_detail='low'`` which downsamples server-side — far
    cheaper than the full describe call. Output is constrained to a
    single word so token cost is negligible.
    """
    hint = (product_hint or "").strip()[:80]
    alt_h = (alt or "").strip()[:60]
    prompt = (
        "Target product: " + (hint or "(unknown)") + ". "
        "alt=" + alt_h + ". "
        "Answer with EXACTLY one word: 'yes' if this image shows that "
        "product, its packaging, or its included accessories; 'no' for "
        "anything else (logos, payment icons, banners, unrelated "
        "products, recommendation tiles, generic site chrome)."
    )
    try:
        from .services.llm import get_llm_service

        llm = get_llm_service()
        resp = llm.predict(
            prompt,
            system_prompt=(
                "You are a strict product-image relevance classifier. "
                "Reply with a single word."
            ),
            image=b64_url,
            image_detail="low",
            max_tokens=4,
            purpose="image_relevance",
        )
        out = (resp.content or "").strip().lower()
        # Strip punctuation / quotes the model sometimes adds.
        out = out.strip(".,'\"`!? \n\r\t")
        if out.startswith("yes"):
            return "yes"
        if out.startswith("no"):
            return "no"
        return ""
    except Exception as exc:
        logger.warning("vision relevance failed: {}", exc)
        return ""


async def _classify_relevance(ref: ImageRef, product_hint: str) -> None:
    """Populate ``ref.relevance`` ('yes'/'no'/'') for one image."""
    if ref.local_path is None or ref.error:
        return
    if ref.mime == "image/svg+xml":
        # SVGs are almost always logos / chrome on retailer pages.
        ref.relevance = "no"
        return
    thumb = _build_vision_thumb(ref.local_path)
    if not thumb:
        ref.relevance = ""
        return
    verdict = await asyncio.to_thread(
        _vision_relevance_sync, thumb, ref.alt, product_hint,
    )
    ref.relevance = verdict


# ---------------------------------------------------------------------- #
# Batched relevance — one LLM call classifies up to N images at once.
# Massive wall-time win on slow corporate gateways: instead of
# (N images × ~3-5s/call ÷ concurrency), it's (⌈N/batch⌉ × ~5-8s/call).
# Per-image marginal token cost is tiny because each thumb is sent at
# image_detail='low'.
# ---------------------------------------------------------------------- #


def _vision_relevance_batch_sync(
    thumbs: list[str], alts: list[str], product_hint: str,
) -> list[str]:
    """Classify a batch of images in one vision call.

    Returns one 'yes'/'no'/'' verdict per input thumbnail in the SAME order.

    Gateway errors are classified carefully:
    * 413 / request_too_large -> ``__OVERSIZE__`` so the wrapper halves the batch.
    * 401/403 Forbidden -> ``__BATCH_UNSUPPORTED__`` so the wrapper stops
      batch retries and falls back to rich per-image vision.
    * 429/5xx -> ``__TEMPFAIL__`` so the wrapper avoids noisy retry storms.
    """
    if not thumbs:
        return []
    hint = (product_hint or "").strip()[:80]
    n = len(thumbs)

    # Build a numbered prompt — the model returns N lines, each "i:yes" or "i:no".
    lines = [f"Target product: {hint or '(unknown)'}.",
             f"You will see {n} numbered images.",
             "For EACH image, decide: does it show that product, its packaging, "
             "or its included accessories?",
             "Reply with EXACTLY one line per image, in order, in the form:",
             "  1: yes",
             "  2: no",
             "  ...",
             "Use 'no' for logos, payment icons, banners, unrelated products, "
             "recommendation tiles, generic site chrome.",
             "",
             "Image hints (alt text):"]
    for i, alt in enumerate(alts, 1):
        a = (alt or "").strip()[:60]
        lines.append(f"  {i}. alt={a!r}")

    try:
        from .services.llm import get_llm_service

        llm = get_llm_service()
        resp = llm.predict(
            "\n".join(lines),
            system_prompt=(
                "You are a strict product-image relevance classifier. "
                "Return one verdict per image, no commentary."
            ),
            images=thumbs,
            image_detail="low",
            max_tokens=4 * n + 16,
            purpose="image_relevance_batch",
        )
        text = (resp.content or "").strip()
    except Exception as exc:
        msg = str(exc)
        lower = msg.lower()
        # Important: corporate gateways often return 403 for disallowed
        # multimodal batch shape, not payload oversize. Do not burn calls
        # retrying 8→4→2→1 on a hard authorization/schema rejection.
        oversize = n > 1 and any(s in lower for s in (
            "413", "payload too large", "request_too_large", "too large", "maximum content",
        ))
        hard_forbidden = any(s in lower for s in ("403", "401", "forbidden", "unauthorized"))
        transient = any(s in lower for s in ("429", "rate limit", "timeout", "temporarily", "500", "502", "503", "504"))
        logger.warning("vision relevance batch failed (n={}): {}", n, exc)
        if hard_forbidden:
            return ["__BATCH_UNSUPPORTED__"] + [""] * (n - 1)
        if oversize:
            return ["__OVERSIZE__"] + [""] * (n - 1)
        if transient:
            return ["__TEMPFAIL__"] + [""] * (n - 1)
        return [""] * n

    # Parse "i: yes/no" lines (loose — model may add markdown bullets).
    out = [""] * n
    for raw in text.splitlines():
        s = raw.strip().lstrip("-*•").strip()
        m = re.match(r"^\(?(\d+)[\)\.\:]\s*(yes|no)\b", s, re.IGNORECASE)
        if not m:
            continue
        idx = int(m.group(1)) - 1
        if 0 <= idx < n:
            out[idx] = m.group(2).lower()
    return out


async def _classify_relevance_batched(
    refs: list[ImageRef], product_hint: str, *, batch_size: int = 8,
) -> None:
    """Run batched relevance over all eligible refs; mutate ref.relevance.

    Adaptive: if the gateway returns an oversize signal (HTTP 403/413/429
    on a multi-image payload), halve the effective batch size and retry
    those images. Repeats until batch_size hits 1 or all images settle.
    """
    pending: list[ImageRef] = []
    pending_thumbs: list[str] = []
    pending_alts: list[str] = []

    for ref in refs:
        if ref.local_path is None or ref.error:
            continue
        if ref.mime == "image/svg+xml":
            ref.relevance = "no"
            continue
        thumb = _build_vision_thumb(ref.local_path)
        if not thumb:
            ref.relevance = ""
            continue
        pending.append(ref)
        pending_thumbs.append(thumb)
        pending_alts.append(ref.alt or "")

    if not pending:
        return

    async def _run_at(bsize: int, refs_p, thumbs_p, alts_p) -> list[ImageRef]:
        """Run one pass at batch size ``bsize``. Returns refs that
        signalled oversize and need to be retried at a smaller bsize.
        """
        # Slice into batches.
        batches: list[tuple[list[ImageRef], list[str], list[str]]] = []
        for i in range(0, len(refs_p), bsize):
            batches.append((
                refs_p[i:i + bsize],
                thumbs_p[i:i + bsize],
                alts_p[i:i + bsize],
            ))

        oversize_refs: list[ImageRef] = []
        oversize_thumbs: list[str] = []
        oversize_alts: list[str] = []

        async def _one_batch(refs_b, thumbs_b, alts_b):
            verdicts = await asyncio.to_thread(
                _vision_relevance_batch_sync, thumbs_b, alts_b, product_hint,
            )
            if verdicts and verdicts[0] == "__BATCH_UNSUPPORTED__":
                logger.warning(
                    "  relevance   : batch vision relevance unsupported/forbidden by gateway; "
                    "disabling batch relevance for this run"
                )
                for r in refs_b:
                    r.relevance = ""
                return
            if verdicts and verdicts[0] == "__TEMPFAIL__":
                logger.warning(
                    "  relevance   : batch vision relevance transient failure; "
                    "leaving {} image(s) unverified for rich-vision fallback",
                    len(refs_b),
                )
                for r in refs_b:
                    r.relevance = ""
                return
            if verdicts and verdicts[0] == "__OVERSIZE__":
                # Stash for retry at smaller bsize. Don't touch
                # ref.relevance — keep it as-is (probably "").
                oversize_refs.extend(refs_b)
                for r, t, a in zip(refs_b, thumbs_b, alts_b):
                    oversize_thumbs.append(t)
                    oversize_alts.append(a)
                return
            for r, v in zip(refs_b, verdicts):
                r.relevance = v

        sem = asyncio.Semaphore(3)

        async def _bound(args):
            async with sem:
                await _one_batch(*args)

        await asyncio.gather(*[_bound(b) for b in batches])

        # Repackage oversize refs with their thumbs/alts for retry.
        # We tracked them in parallel above.
        if oversize_refs:
            return list(zip(oversize_refs, oversize_thumbs, oversize_alts))  # type: ignore[return-value]
        return []

    cur = list(zip(pending, pending_thumbs, pending_alts))
    bsize = batch_size
    while cur and bsize >= 1:
        refs_p = [t[0] for t in cur]
        thumbs_p = [t[1] for t in cur]
        alts_p = [t[2] for t in cur]
        retry = await _run_at(bsize, refs_p, thumbs_p, alts_p)
        if not retry:
            break
        new_bsize = max(1, bsize // 2)
        if new_bsize == bsize:
            # Already at 1 and still oversize — give up; verdicts stay "".
            logger.warning(
                "  relevance   : gateway rejects even single-image calls; "
                "leaving {} image(s) unverified",
                len(retry),
            )
            break
        logger.warning(
            "  relevance   : payload too large on batch={} → retrying {} "
            "image(s) at batch={}",
            bsize, len(retry), new_bsize,
        )
        cur = retry  # type: ignore[assignment]
        bsize = new_bsize


async def _describe_one(ref: ImageRef, product_hint: str) -> None:
    if ref.local_path is None or ref.error:
        return
    if ref.mime == "image/svg+xml":
        ref.description = "(svg vector — skipped vision)"
        return
    thumb = _build_vision_thumb(ref.local_path)
    if not thumb:
        ref.description = "(thumbnail build failed)"
        return
    desc = await asyncio.to_thread(
        _vision_describe_sync, thumb, ref.alt, product_hint,
    )
    ref.description = desc


# ----------------------------------------------------------------------
# Public entry
# ----------------------------------------------------------------------
async def download_and_describe(
    image_urls: list[tuple[str, str]],
    *,
    referer: str,
    out_dir: Path,
    max_images: int = 30,
    vision_max: int = 12,
    download_concurrency: int = 6,
    vision_concurrency: int = 3,
    product_hint: str = "",
) -> list[ImageRef]:
    """Download all images, then run LLM vision on the most informative ones.

    Args:
        image_urls: (url, alt) pairs in document order.
        referer: original page URL (some CDNs require this).
        out_dir: scrape artifact output directory; ``images/`` is created here.
        max_images: hard cap on number of distinct images downloaded.
        vision_max: hard cap on number of images sent through GPT-4o vision.
        product_hint: short "<brand> <product>" string used by the
            relevance gate; vision-flagged unrelated images are deleted
            from disk after the vision pass.
    """
    images_dir = out_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    seen: set[str] = set()
    queue: list[tuple[str, str]] = []
    n_skipped_chrome = 0
    for url, alt in image_urls:
        if url in seen:
            continue
        if _should_skip(url, alt):
            n_skipped_chrome += 1
            continue
        seen.add(url)
        queue.append((url, alt))
        if len(queue) >= max_images:
            break

    logger.info(
        "  url-filter  : {} candidates → {} unique  ({} chrome/icons skipped, "
        "cap={})",
        len(image_urls), len(queue), n_skipped_chrome, max_images,
    )
    if not queue:
        return []

    sem = asyncio.Semaphore(download_concurrency)

    async def _bound(client, idx, url, alt):
        async with sem:
            return await _download_one(client, url, alt, referer, idx, images_dir)

    import time as _time
    t_dl = _time.monotonic()
    async with httpx.AsyncClient(timeout=15.0, verify=SSL_CTX) as client:
        refs = await asyncio.gather(
            *[_bound(client, i, u, a) for i, (u, a) in enumerate(queue, start=1)]
        )
    refs = list(refs)
    dl_secs = _time.monotonic() - t_dl
    n_dl_ok = sum(1 for r in refs if r.local_path is not None)
    n_dl_err = sum(1 for r in refs if r.error)
    total_bytes = sum(r.bytes_size for r in refs if r.local_path is not None)
    logger.info(
        "  download    : {}/{} ok ({:,}KB total) in {:.2f}s  "
        "(concurrency={}, errors={})",
        n_dl_ok, len(queue), total_bytes // 1024, dl_secs,
        download_concurrency, n_dl_err,
    )
    if n_dl_err:
        # Show a couple of error reasons (anonymously bucketed).
        err_buckets: dict[str, int] = {}
        for r in refs:
            if r.error:
                key = r.error.split(":")[0][:40]
                err_buckets[key] = err_buckets.get(key, 0) + 1
        for k, v in sorted(err_buckets.items(), key=lambda x: -x[1])[:3]:
            logger.info("    error[{}×] : {}", v, k)

    # ---- Deduplicate by content hash ----------------------------------
    # CDNs commonly serve the same image at many URLs (size variants,
    # cache-busting query strings, mirrors). Trust the bytes, not the URL.
    seen_sha: dict[str, ImageRef] = {}
    deduped: list[ImageRef] = []
    dropped = 0
    for r in refs:
        if r.local_path is None or not r.sha8:
            deduped.append(r)  # failures kept for manifest visibility
            continue
        first = seen_sha.get(r.sha8)
        if first is None:
            seen_sha[r.sha8] = r
            deduped.append(r)
            continue
        # Duplicate content — delete the redundant file, fold alt into
        # the first occurrence so no information is lost.
        try:
            r.local_path.unlink()
        except OSError:
            pass
        if r.alt and r.alt not in (first.alt or ""):
            first.alt = (first.alt + " | " + r.alt).strip(" |") if first.alt else r.alt
        dropped += 1
    refs = deduped

    # ---- Perceptual dedup (pHash, Hamming ≤ 6 / 64 bits) --------------
    # CDNs serve the same hero shot at multiple sizes / re-encodings
    # (`_800x800.jpg`, `_400x400.jpg`, WebP mirror, etc.) — different
    # bytes, identical pixels. SHA1 can't see that; pHash can.
    # Threshold 6 bits is the classic near-duplicate cutoff: tight
    # enough to keep genuinely different angles/views as separate
    # entries, loose enough to collapse resize/recompress variants.
    # When duplicates are found, we keep the highest-resolution copy
    # (largest pixel area, ties broken by file size).
    def _hamming(a: str, b: str) -> int:
        try:
            return bin(int(a, 16) ^ int(b, 16)).count("1")
        except Exception:  # noqa: BLE001
            return 64

    kept: list[ImageRef] = []
    phash_dropped = 0
    for r in refs:
        if r.local_path is None or not r.phash:
            kept.append(r)
            continue
        match = None
        for k in kept:
            if k.local_path is None or not k.phash:
                continue
            if _hamming(r.phash, k.phash) <= 6:
                match = k
                break
        if match is None:
            kept.append(r)
            continue
        # Decide which copy to keep: prefer larger pixel area, then bytes.
        r_score = (r.width * r.height, r.bytes_size)
        k_score = (match.width * match.height, match.bytes_size)
        if r_score > k_score:
            # Newcomer is higher-res — replace the previously-kept one.
            try:
                if match.local_path:
                    match.local_path.unlink()
            except OSError:
                pass
            if match.alt and match.alt not in (r.alt or ""):
                r.alt = (r.alt + " | " + match.alt).strip(" |") if r.alt else match.alt
            idx = kept.index(match)
            kept[idx] = r
        else:
            try:
                r.local_path.unlink()
            except OSError:
                pass
            if r.alt and r.alt not in (match.alt or ""):
                match.alt = (match.alt + " | " + r.alt).strip(" |") if match.alt else r.alt
        phash_dropped += 1
    refs = kept

    ok = [r for r in refs if r.local_path is not None]
    logger.info(
        "  dedup       : {} unique by sha1 ({} byte-identical duplicates dropped)",
        len(ok) + phash_dropped, dropped,
    )
    if phash_dropped:
        logger.info(
            "  dedup pHash : {} resize/re-encode duplicates collapsed → {} unique photos",
            phash_dropped, len(ok),
        )

    # Pick the largest images for vision (heuristic: bigger == product hero shots)
    by_area = sorted(
        ok,
        key=lambda r: (r.width * r.height, r.bytes_size),
        reverse=True,
    )

    # Optional no-LLM mode for environments where only raw scrape artifacts
    # are needed. Images remain downloaded/deduped; vision.md may be absent.
    import os as _os
    llm_enabled = _os.getenv("PCA_LLM_ENABLED", "1").lower() in (
        "1", "true", "yes", "y", "on"
    )
    vision_enabled = llm_enabled and _os.getenv("PCA_LLM_VISION_ENABLED", "1").lower() in (
        "1", "true", "yes", "y", "on"
    )
    if not vision_enabled:
        logger.info(
            "  vision      : skipped because PCA_LLM_ENABLED or PCA_LLM_VISION_ENABLED is false"
        )
        return refs

    # ---- Relevance pre-pass: cheap RELATED yes/no on EVERY image -------
    # BATCHED: one LLM call per N images instead of one per image.
    # Adaptive: starts at PCA_RELEVANCE_BATCH (default 8); if the
    # gateway returns HTTP 403/413/429 (payload too large), the batch
    # is automatically halved (8 → 4 → 2 → 1) and retried.
    batch_enabled = _os.getenv("PCA_RELEVANCE_BATCH_ENABLED", "1").lower() in (
        "1", "true", "yes", "y", "on"
    )
    rel_batch = 1 if not batch_enabled else max(1, int(_os.getenv("PCA_RELEVANCE_BATCH", "8")))

    t_rel = _time.monotonic()
    if rel_batch == 1:
        rel_sem = asyncio.Semaphore(vision_concurrency)

        async def _rel_bound(ref):
            async with rel_sem:
                await _classify_relevance(ref, product_hint)

        await asyncio.gather(*[_rel_bound(r) for r in ok])
    else:
        await _classify_relevance_batched(
            ok, product_hint, batch_size=rel_batch,
        )
    n_yes = sum(1 for r in ok if r.relevance == "yes")
    n_no = sum(1 for r in ok if r.relevance == "no")
    n_unk = sum(1 for r in ok if r.relevance == "")
    logger.info(
        "  relevance   : {} yes / {} no / {} unknown of {} in {:.2f}s "
        "(batch={})",
        n_yes, n_no, n_unk, len(ok), _time.monotonic() - t_rel, rel_batch,
    )

    # Pruning gate. Two regimes:
    #
    #  * NORMAL — classifier worked for most images: drop everything
    #    not explicitly 'yes' (fail-closed). Keeps ``images/`` clean.
    #  * DEGRADED — classifier failed on >50% of images (e.g. gateway
    #    HTTP 403 / 429 / 5xx storm): only drop the explicit 'no's,
    #    keep the unknowns. Better to ship a few banners than to
    #    silently delete every product image because the relevance
    #    sidecar was unhealthy. The rich-describe pass below applies a
    #    second relevance check on the picks anyway.
    degraded = (n_yes + n_no) < (len(ok) // 2)
    if degraded and len(ok) > 2:
        logger.warning(
            "  pruning     : DEGRADED mode — classifier verdicts <50%% "
            "({} of {}); keeping unknowns to preserve image evidence",
            n_yes + n_no, len(ok),
        )

    n_dropped = 0
    for r in ok:
        if r.relevance == "yes":
            continue
        if r.relevance == "" and degraded:
            # Keep the unknown — gateway flaked, don't punish the page.
            continue
        if r.local_path is not None:
            try:
                r.local_path.unlink()
            except OSError:
                pass
        r.local_path = None
        r.error = (
            "unrelated to product (vision verdict: no)"
            if r.relevance == "no"
            else "relevance unverified — dropped fail-closed"
        )
        n_dropped += 1
    if n_dropped:
        logger.info(
            "  pruned      : deleted {} non-product image(s) from disk",
            n_dropped,
        )

    # Survivors carry forward to the rich describe pass.
    survivors = [r for r in by_area if r.local_path is not None]
    picks = survivors[:vision_max]
    pick_set = {id(p) for p in picks}
    if picks:
        logger.info(
            "  vision pick : top {} of {} survivors by area  "
            "(largest {}×{}, smallest {}×{})",
            len(picks), len(survivors),
            picks[0].width, picks[0].height,
            picks[-1].width, picks[-1].height,
        )

    vsem = asyncio.Semaphore(vision_concurrency)

    async def _vbound(ref):
        if id(ref) not in pick_set:
            return
        async with vsem:
            await _describe_one(ref, product_hint)

    t_v = _time.monotonic()
    await asyncio.gather(*[_vbound(r) for r in refs])
    n_described = sum(1 for r in refs if r.description)
    logger.info(
        "  vision LLM  : {}/{} described in {:.2f}s  (concurrency={})",
        n_described, len(picks), _time.monotonic() - t_v, vision_concurrency,
    )

    # Final cleanliness gate.
    # For this project, visual evidence is mandatory. Do NOT delete all useful
    # downloaded images just because the rich vision-description sidecar failed.
    # Keep confirmed product images. In degraded vision mode, keep unverified
    # high-resolution survivors as manual-review visual evidence so downstream
    # coding still has pixels to inspect.
    _REL_RE = re.compile(r"^\s*RELATED\s*:\s*(yes|no)\b", re.IGNORECASE)
    keep_unverified = os.getenv("PCA_IMAGE_KEEP_UNVERIFIED_ON_VISION_FAILURE", "1").lower() in {"1", "true", "yes", "y", "on"}
    n_final_dropped = 0
    n_final_kept = 0
    for r in refs:
        if r.local_path is None:
            continue
        rel_match = _REL_RE.match(r.description or "")
        rich_verdict = rel_match.group(1).lower() if rel_match else ""
        keep = False
        if rich_verdict == "yes" or r.relevance == "yes":
            keep = True
            r.relevance = "yes"
            if not r.description:
                r.description = (
                    "RELATED: yes\n"
                    "- Product image retained by the relevance gate.\n"
                    "- Rich vision description was unavailable, but the image file is preserved for downstream inspection."
                )
        elif r.relevance == "" and degraded and keep_unverified:
            keep = True
            r.relevance = "unverified_kept"
            if not r.description:
                r.description = (
                    "RELATED: unverified\n"
                    "- Image retained because the vision relevance/description gateway was degraded.\n"
                    "- Manual review or downstream vision should inspect this image before using it as product evidence."
                )
        if keep:
            n_final_kept += 1
            continue
        try:
            r.local_path.unlink()
        except OSError:
            pass
        r.local_path = None
        if rich_verdict == "no":
            r.error = "unrelated to product (rich-describe verdict)"
            r.relevance = "no"
            r.description = ""
        elif r.relevance == "no":
            r.error = "unrelated to product (relevance verdict)"
            r.description = ""
        elif not r.description:
            r.error = "not vision-described; removed from clean images folder"
        else:
            r.error = "vision verdict missing/uncertain; removed from clean images folder"
        n_final_dropped += 1
    if n_final_dropped or n_final_kept:
        logger.info(
            "  final clean : kept {} image evidence file(s); removed {} candidate file(s)",
            n_final_kept, n_final_dropped,
        )

    # vision.md — business-readable, table-first summary of retained images.
    final_images = [r for r in refs if r.local_path is not None and r.description]
    vis_lines = [
        "# Visual Evidence Summary",
        "",
        "Only final vision-confirmed product images are listed. Candidate images removed during cleanup remain auditable in `manifests/image_manifest.json`.",
        "",
        "## Decision table",
        "",
        "| # | File | Decision | Visible product evidence | Alt text |",
        "|---:|---|---|---|---|",
    ]
    for i, r in enumerate(final_images, start=1):
        rel = r.local_path.relative_to(out_dir) if r.local_path else ""
        desc = re.sub(r"^\s*RELATED\s*:\s*yes\s*", "", r.description or "", flags=re.I).strip()
        desc = "<br>".join(
            line.strip().lstrip("-•* ").replace("|", "\\|")
            for line in desc.splitlines() if line.strip()
        )[:900]
        alt = (r.alt or "").replace("|", "\\|")[:180]
        vis_lines.append(f"| {i} | `{rel}` | Keep — product/packaging evidence | {desc or '(description empty)'} | {alt} |")
    if final_images:
        vis_lines.extend([
            "",
            "## Image-level observations",
            "",
        ])
        for i, r in enumerate(final_images, start=1):
            rel = r.local_path.relative_to(out_dir) if r.local_path else r.url
            vis_lines.append(f"### Image {i:03d} — `{rel}`")
            vis_lines.append("")
            vis_lines.append((r.description or "").strip())
            vis_lines.append("")
        (out_dir / "vision.md").write_text("\n".join(vis_lines).strip() + "\n", encoding="utf-8")

    return refs


async def capture_screenshot_fallback(
    *,
    page_url: str,
    out_dir: Path,
    product_hint: str = "",
    timeout: float = 25.0,
    full_page: bool = False,
) -> ImageRef:
    """Capture a page screenshot as last-resort visual evidence.

    This is not treated as a clean product-gallery image. It is a rescue artifact
    so downstream vision/manual review still has pixels when CDN image downloads
    fail. It uses Playwright directly because Crawl4AI's public result object does
    not consistently expose screenshot bytes across versions.
    """
    images_dir = out_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    ref = ImageRef(url=page_url, alt="page screenshot fallback")
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # pragma: no cover
        ref.error = f"screenshot fallback unavailable: {type(exc).__name__}: {exc}"
        return ref
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=BROWSER_HEADERS.get("User-Agent"),
                viewport={"width": 1366, "height": 1100},
                extra_http_headers={
                    "Accept-Language": os.getenv("PCA_ACCEPT_LANGUAGE", "en-US,en;q=0.9"),
                },
            )
            page = await context.new_page()
            await page.goto(page_url, wait_until="domcontentloaded", timeout=timeout * 1000)
            await page.wait_for_timeout(2500)
            # Try to place the product area/galleries in view, without assuming retailer-specific selectors.
            for sel in ["main", "[role=main]", "#dp", "#ppd", "[data-testid*=product]", ".product", ".pdp"]:
                try:
                    loc = page.locator(sel).first
                    if await loc.count():
                        await loc.scroll_into_view_if_needed(timeout=1000)
                        break
                except Exception:
                    pass
            path = images_dir / "screenshot_fallback.png"
            await page.screenshot(path=str(path), full_page=full_page)
            await context.close()
            await browser.close()
        data = path.read_bytes()
        with Image.open(io.BytesIO(data)) as im:
            ref.width, ref.height = im.size
            ref.phash = _compute_phash(im)
        ref.local_path = path
        ref.bytes_size = len(data)
        ref.mime = "image/png"
        ref.sha8 = hashlib.sha1(data).hexdigest()[:8]
        ref.download_source = "screenshot_fallback"
        ref.relevance = "screenshot_fallback"
        ref.description = (
            "RELATED: screenshot_fallback\n"
            "- Page screenshot retained as last-resort visual evidence because clean product image recovery failed.\n"
            "- This is not a clean gallery image; downstream vision/manual review must inspect it cautiously."
        )
        return ref
    except Exception as exc:  # noqa: BLE001
        ref.error = f"screenshot fallback failed: {type(exc).__name__}: {exc}"
        return ref


__all__ = ["download_and_describe", "capture_screenshot_fallback"]
