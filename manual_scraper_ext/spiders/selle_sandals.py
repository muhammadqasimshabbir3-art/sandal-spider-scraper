"""
selle_sandals.py
~~~~~~~~~~~~~~~~
Crawls https://www.selle-sandals.com/collections/all and downloads
ALL gallery images for every product variant, organised per colour.

Key findings from reverse-engineering the site HTML:
  - Images use protocol-relative URLs: //www.selle-sandals.com/cdn/shop/…
    (NOT cdn.shopify.com). The check must look for /cdn/shop/ or the store
    domain — not cdn.shopify.com.
  - Variants live in <script type="application/json"> inside the
    <variant-radios> custom element — NOT in `var meta = {}`.
  - Each colour has its own featured_image in the variant JSON; we collect
    those colour-specific images FIRST, then append any remaining gallery
    images so every colour gets all relevant photos.
  - Gallery images appear twice in the HTML (src and inside srcset) at
    different widths; we deduplicate by base filename.
"""

import re
import json
from urllib.parse import urlparse

import scrapy

from manual_scraper_ext.items import SelleSandalsItem


# Image domains / path patterns this store uses
_IMG_PATTERNS = (
    "/cdn/shop/",     # www.selle-sandals.com/cdn/shop/…
    "cdn.shopify.com",  # fallback for standard Shopify CDN
)


def _is_product_image(src: str) -> bool:
    """Return True when src looks like a product photo (not a logo/icon)."""
    if not any(p in src for p in _IMG_PATTERNS):
        return False
    low = src.lower()
    if any(x in low for x in (".svg", "icon", "logo", "badge", "spinner",
                               "placeholder", "pixel", "tracking")):
        return False
    return True


class SelleSandalsSpider(scrapy.Spider):
    name = "selle-sandals"
    start_urls = ["https://www.selle-sandals.com/collections/all"]
    _page = 1

    custom_settings = {
        "CONCURRENT_REQUESTS": 2,
        "DOWNLOAD_DELAY": 0.8,

        "ITEM_PIPELINES": {
            "manual_scraper_ext.pipelines.CustomImagesPipeline": 1,
        },
        "IMAGES_STORE": "images",
        "IMAGES_URLS_FIELD": "image_urls",
        "IMAGES_RESULT_FIELD": "images",

        "USER_AGENT": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "DEFAULT_REQUEST_HEADERS": {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            # brotlicffi is installed so we can safely accept br encoding
            "Accept-Encoding": "gzip, deflate, br",
        },

        "HTTPCACHE_ENABLED": False,
        "COOKIES_ENABLED": True,
        "RETRY_HTTP_CODES": [500, 502, 503, 504, 408],
    }

    # ─────────────────────────────────────────────────────────────────────────
    # Utilities
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _to_https(url: str) -> str:
        """Convert protocol-relative //example.com/… to https://example.com/…"""
        if url.startswith("//"):
            return "https:" + url
        return url

    @staticmethod
    def _strip_resize(url: str) -> str:
        """
        Remove Shopify image resize params so we get the full-res original.
        ?v=1739817716&width=1946  →  ?v=1739817716
        """
        url = re.sub(r'[&?]width=\d+', '', url)
        url = re.sub(r'[&?]height=\d+', '', url)
        url = re.sub(r'[&?]crop=\w+', '', url)
        url = re.sub(r'[?&]+$', '', url)
        return url

    @staticmethod
    def _base_filename(url: str) -> str:
        """Filename portion only, no query string — used for deduplication."""
        path = urlparse(url).path
        return path.rsplit("/", 1)[-1].lower()

    def _clean(self, url: str) -> str:
        """Protocol-fix + strip resize params."""
        return self._strip_resize(self._to_https(url))

    def _add(self, lst: list, url: str, seen: set) -> None:
        """Append url to lst if its base filename hasn't been seen yet."""
        fname = self._base_filename(url)
        if fname and fname not in seen:
            seen.add(fname)
            lst.append(url)

    # ─────────────────────────────────────────────────────────────────────────
    # Image extraction
    # ─────────────────────────────────────────────────────────────────────────

    def _gallery_images(self, response) -> list[str]:
        """
        Collect every unique product image URL from the page.

        Strategy (in priority order):
          1. <img src="…"> and <img srcset="…"> — covers all visible images
          2. data-src / data-srcset for lazy-loaded imgs
          3. og:image meta tag
          4. Raw regex over full HTML (last resort)
        """
        seen: set[str] = set()
        urls: list[str] = []

        # ── 1. <img src> and srcset ──────────────────────────────────────────
        for img in response.css("img"):
            # src attribute
            src = img.attrib.get("src", "")
            if src and _is_product_image(src):
                self._add(urls, self._clean(response.urljoin(src)), seen)

            # srcset attribute  (e.g. "//…/img.jpg?width=165 165w, //…/img.jpg?width=360 360w")
            srcset = img.attrib.get("srcset", "")
            if srcset:
                for part in srcset.split(","):
                    candidate = part.strip().split()[0]  # take URL, drop descriptor
                    if candidate and _is_product_image(candidate):
                        self._add(urls, self._clean(response.urljoin(candidate)), seen)

        # ── 2. Lazy-load attributes ──────────────────────────────────────────
        for attr in ("data-src", "data-zoom-src", "data-image"):
            for val in response.css(f"img::attr({attr})").getall():
                if _is_product_image(val):
                    self._add(urls, self._clean(response.urljoin(val)), seen)

        # ── 3. og:image meta ────────────────────────────────────────────────
        og = response.css('meta[property="og:image"]::attr(content)').get() or ""
        if og and _is_product_image(og):
            self._add(urls, self._clean(response.urljoin(og)), seen)

        # ── 4. Raw regex scan (last resort) ─────────────────────────────────
        if not urls:
            self.logger.debug("Falling back to regex image scan for %s", response.url)
            raw = re.findall(
                r'(?:https?:)?//[^\s"\'\\>]+/cdn/shop/[^\s"\'\\>]+\.(?:jpg|jpeg|png|webp|gif)',
                response.text,
                re.I,
            )
            for u in raw:
                if _is_product_image(u):
                    self._add(urls, self._clean(response.urljoin(u)), seen)

        return urls

    def _color_image(self, variant: dict, response) -> str | None:
        """
        Return the variant's own featured_image URL (colour-specific), or None.
        Handles both variant JSON formats Shopify uses.
        """
        fi = variant.get("featured_image") or {}
        src = fi.get("src", "")
        if src and _is_product_image(src):
            return self._clean(response.urljoin(src))
        return None

    # ─────────────────────────────────────────────────────────────────────────
    # Variant / colour parsing
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _is_color(value: str) -> bool:
        """True when the string is NOT a pure number (i.e. it looks like a colour)."""
        return not re.match(r"^\d+(\.\d+)?$", value.strip())

    def _parse_variants(self, response) -> list[dict]:
        """
        Extract variant list from the page.

        This Shopify store (Dawn theme) puts variants inside:
            <variant-radios …>
              <script type="application/json">[ … ]</script>
            </variant-radios>

        Fallback: look for the older `var meta = {…}` JS pattern.
        """
        # ── Primary: <variant-radios> / <variant-selects> JSON ───────────────
        json_text = response.css(
            "variant-radios script[type='application/json']::text, "
            "variant-selects script[type='application/json']::text"
        ).get()
        if json_text:
            try:
                data = json.loads(json_text)
                if isinstance(data, list) and data:
                    return data
            except json.JSONDecodeError:
                self.logger.warning("JSON decode error for variants on %s", response.url)

        # ── Fallback: var meta = {…} ─────────────────────────────────────────
        script = response.xpath(
            "//script[contains(., 'var meta =')]/text()"
        ).get()
        if script:
            match = re.search(r"var meta = (\{.*?\});", script, re.S)
            if match:
                try:
                    meta_json = json.loads(match.group(1))
                    variants = meta_json.get("product", {}).get("variants", [])
                    if variants:
                        return variants
                except json.JSONDecodeError:
                    pass

        # ── Last resort: colour radio buttons in HTML ─────────────────────────
        colors = response.css(
            'input[name="Color"]::attr(value), '
            'input[name="colour"]::attr(value), '
            'input[name="color"]::attr(value)'
        ).getall()
        if colors:
            return [{"public_title": c, "id": f"html_{i}", "featured_image": None}
                    for i, c in enumerate(colors)]

        return [{"public_title": "Default", "id": "default", "featured_image": None}]

    def _unique_colors(self, variants: list[dict]) -> list[tuple]:
        """
        Return (color, variant_id, variant_dict) for the FIRST variant of each colour.

        Shopify public_title format examples:
          "41 / Green", "Green / 41", "Brown", "41"
        """
        seen: set[str] = set()
        result: list[tuple] = []

        for v in variants:
            public_title = (v.get("public_title") or "").strip()
            option1 = (v.get("option1") or "").strip()
            option2 = (v.get("option2") or "").strip()
            option3 = (v.get("option3") or "").strip()

            # Try to identify colour from the known option fields first
            color = ""
            for opt in (option1, option2, option3):
                if opt and self._is_color(opt):
                    color = opt
                    break

            # Fall back to parsing public_title (e.g. "41 / Green")
            if not color and " / " in public_title:
                parts = [p.strip() for p in public_title.split("/")]
                for part in parts:
                    if self._is_color(part):
                        color = part
                        break

            if not color:
                color = public_title or "Default"

            key = color.lower()
            if key not in seen:
                seen.add(key)
                result.append((color, v.get("id"), v))

        return result

    # ─────────────────────────────────────────────────────────────────────────
    # Collection page  →  follow product links + paginate
    # ─────────────────────────────────────────────────────────────────────────

    def parse(self, response):
        # Guard: skip non-text (e.g. still-encoded binary) responses
        if not hasattr(response, 'text'):
            self.logger.warning(
                "Non-text response on collection page %s — skipping", response.url
            )
            return
        try:
            _ = response.text  # trigger decode; raises if still not text
        except Exception as exc:
            self.logger.warning(
                "Cannot decode response on %s (%s) — skipping", response.url, exc
            )
            return

        # Product card heading links (Dawn theme)
        product_links = response.xpath(
            '//h3[contains(@class,"card__heading")]/a/@href'
        ).getall()

        # Fallback: any /products/ href
        if not product_links:
            product_links = response.css(
                'a[href*="/products/"]::attr(href)'
            ).re(r'^/products/[^/?#]+$')

        for link in set(product_links):
            yield response.follow(link, callback=self.parse_product_page)

        self.logger.info(
            "[selle-sandals] Found %d product links on %s",
            len(set(product_links)), response.url,
        )

        # Pagination — prefer "Next page" link, else try ?page=N (up to 10)
        next_page = (
            response.xpath('//a[@aria-label="Next page"]/@href').get()
            or response.css('a.next::attr(href)').get()
            or response.css('a[rel="next"]::attr(href)').get()
        )
        if next_page:
            yield response.follow(next_page, callback=self.parse)
        else:
            self._page += 1
            if self._page <= 10:
                next_url = response.urljoin(f"?page={self._page}")
                # Only continue if the page actually has products
                yield scrapy.Request(next_url, callback=self.parse)

    # ─────────────────────────────────────────────────────────────────────────
    # Product page  →  yield one item per unique colour
    # ─────────────────────────────────────────────────────────────────────────

    def parse_product_page(self, response):
        # ── 1. Product title ─────────────────────────────────────────────────
        title = (
            response.css("h1.product__title::text").get()
            or response.css("h1::text").get()
            or response.css('meta[property="og:title"]::attr(content)').get()
            or ""
        ).strip()

        if " – " in title:
            title = title.split(" – ")[0].strip()

        if not title:
            self.logger.warning("No title found on %s", response.url)
            return

        # ── 2. ALL gallery images ─────────────────────────────────────────────
        all_images = self._gallery_images(response)
        all_images_set = set(self._base_filename(u) for u in all_images)

        self.logger.info(
            "[%s] %d unique gallery images on %s",
            title, len(all_images), response.url,
        )

        if not all_images:
            self.logger.warning("Zero images for '%s' at %s", title, response.url)

        # ── 3. Variants ───────────────────────────────────────────────────────
        variants = self._parse_variants(response)
        color_variants = self._unique_colors(variants)

        self.logger.info(
            "[%s] %d colour variant(s): %s",
            title,
            len(color_variants),
            [c for c, _, __ in color_variants],
        )

        # ── 4. Yield one item per unique colour ───────────────────────────────
        for color, variant_id, variant_dict in color_variants:
            # Build a colour-aware image list:
            # Start with the variant's own featured image (if it's unique)
            color_urls: list[str] = []
            color_seen: set[str] = set()

            featured = self._color_image(variant_dict, response)
            if featured:
                self._add(color_urls, featured, color_seen)

            # Append all remaining gallery images not already included
            for url in all_images:
                self._add(color_urls, url, color_seen)

            item = SelleSandalsItem()
            item["title"] = title
            item["url"] = f"{response.url}?variant={variant_id}"
            item["source"] = self.name
            item["meta"] = {
                "variant_id": variant_id,
                "color": color,
                "title": title,
            }
            item["image_urls"] = color_urls

            yield item
