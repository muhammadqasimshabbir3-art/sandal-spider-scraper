"""
image_utils.py
~~~~~~~~~~~~~~
Shared image URL utilities used by every spider in the framework.

Extracted from selle_sandals.py so all spiders can reuse the same
protocol-fix, resize-strip, deduplication, and filtering logic.
"""

import re
from urllib.parse import urlparse


# ─────────────────────────────────────────────────────────────────────────────
# Generic noise patterns that are NEVER product photos
# ─────────────────────────────────────────────────────────────────────────────

_SKIP_PATTERNS: tuple[str, ...] = (
    ".svg",
    "icon",
    "logo",
    "badge",
    "spinner",
    "placeholder",
    "noimage",
    "noimagesmall",
    "pixel",
    "tracking",
    "transparent",
    "blank",
    "1x1",
    "social",
    "payment",
    "flag",
    "arrow",
    "bullet",
    "star-",
    "rating",
    "review",
    "avatar",
    "profile",
    "video-thumb",
    "play-button",
    "lifestyle",    # lifestyle/editorial photos
    "editorial",
    "banner",
    "hero-",
    "advertisement",
    "ad-",
    "-ad.",
)

_VALID_EXTENSIONS: frozenset[str] = frozenset(
    ("jpg", "jpeg", "png", "webp", "gif")
)


def to_https(url: str) -> str:
    """Convert protocol-relative ``//host/path`` to ``https://host/path``."""
    if url.startswith("//"):
        return "https:" + url
    return url


def strip_resize_params(url: str) -> str:
    """
    Remove resize query-string parameters so we retrieve the full-resolution
    original instead of a thumbnail.

    Works for Shopify, Zappos CDN, Akamai, and most proprietary CDNs.

    Examples::

        ?v=123&width=1946   →   ?v=123
        _800x800.jpg        →   .jpg    (filename suffix, NOT modified here)
    """
    url = re.sub(r"[&?]width=\d+", "", url)
    url = re.sub(r"[&?]height=\d+", "", url)
    url = re.sub(r"[&?]crop=\w+", "", url)
    url = re.sub(r"[&?]fit=\w+", "", url)
    url = re.sub(r"[&?]quality=\d+", "", url)
    url = re.sub(r"[&?]format=\w+", "", url)
    url = re.sub(r"[&?]auto=\w+", "", url)
    url = re.sub(r"[&?]dpr=[\d.]+", "", url)
    url = re.sub(r"[?&]+$", "", url)
    return url


def base_filename(url: str) -> str:
    """
    Return only the filename portion of a URL, without query string.

    Used for deduplication: two URLs pointing to the same physical file will
    share the same ``base_filename``.
    """
    path = urlparse(url).path
    return path.rsplit("/", 1)[-1].lower()


def is_valid_extension(url: str) -> bool:
    """Return True when the URL appears to point to a raster image file."""
    path = urlparse(url).path.split("?")[0]
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return ext in _VALID_EXTENSIONS


def is_noise(url: str) -> bool:
    """
    Return True when the URL matches a known noise pattern (logo, icon, etc.)
    that should be excluded from the product gallery.
    """
    low = url.lower()
    return any(p in low for p in _SKIP_PATTERNS)


def is_product_image(url: str, cdn_patterns: tuple[str, ...] = ()) -> bool:
    """
    Return True when *url* looks like a genuine product photo.

    Parameters
    ----------
    url:
        The image URL to evaluate.
    cdn_patterns:
        Optional site-specific CDN substrings that a valid product image URL
        must contain at least one of.  Pass an empty tuple to skip this check.
    """
    if not url:
        return False
    if not is_valid_extension(url):
        return False
    if is_noise(url):
        return False
    if cdn_patterns and not any(p in url for p in cdn_patterns):
        return False
    return True


def clean_url(url: str) -> str:
    """Apply ``to_https`` + ``strip_resize_params`` in one call."""
    return strip_resize_params(to_https(url))


def add_unique(lst: list[str], url: str, seen: set[str]) -> None:
    """
    Append *url* to *lst* only if its ``base_filename`` has not been seen yet.

    Mutates both *lst* and *seen* in place.
    """
    fname = base_filename(url)
    if fname and fname not in seen:
        seen.add(fname)
        lst.append(url)


def collect_images_from_response(
    response,
    cdn_patterns: tuple[str, ...] = (),
    logger=None,
) -> list[str]:
    """
    Universal image collector that works against any HTML response.

    Strategy (in priority order):
      1. ``<img src>`` and ``srcset`` attributes
      2. Lazy-load data attributes (``data-src``, ``data-zoom-src``, ``data-image``)
      3. ``og:image`` meta tag
      4. Raw regex scan over full HTML (last resort)

    Parameters
    ----------
    response:
        A Scrapy ``HtmlResponse`` object.
    cdn_patterns:
        Site-specific CDN URL substrings; passed directly to
        :func:`is_product_image`.
    logger:
        Optional logger for debug/warning messages.

    Returns
    -------
    list[str]
        De-duplicated list of absolute, full-resolution image URLs.
    """
    seen: set[str] = set()
    urls: list[str] = []

    def add(url: str) -> None:
        url = clean_url(response.urljoin(url))
        if is_product_image(url, cdn_patterns):
            add_unique(urls, url, seen)

    # ── 1. <img src> and srcset ───────────────────────────────────────────────
    for img in response.css("img"):
        src = img.attrib.get("src", "")
        if src:
            add(src)

        srcset = img.attrib.get("srcset", "")
        if srcset:
            for part in srcset.split(","):
                candidate = part.strip().split()[0]
                if candidate:
                    add(candidate)

    # ── 2. Lazy-load attributes ───────────────────────────────────────────────
    for attr in ("data-src", "data-zoom-src", "data-image", "data-lazy-src",
                 "data-original", "data-url"):
        for val in response.css(f"img::attr({attr})").getall():
            if val:
                add(val)

    # ── 3. og:image ───────────────────────────────────────────────────────────
    og = response.css('meta[property="og:image"]::attr(content)').get() or ""
    if og:
        add(og)

    # ── 4. Raw regex scan (last resort) ───────────────────────────────────────
    if not urls:
        if logger:
            logger.debug("Falling back to regex image scan for %s", response.url)
        pattern = (
            r'(?:https?:)?//[^\s"\'\\>]+'
            r'\.(?:jpg|jpeg|png|webp|gif)(?:[?#][^\s"\'\\>]*)?'
        )
        for u in re.findall(pattern, response.text, re.I):
            add(u)

    return urls
