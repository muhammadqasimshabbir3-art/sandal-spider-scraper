"""
underarmour.py
~~~~~~~~~~~~~~
Spider for https://www.underarmour.com/ — Men's sandals and slides.

Reverse-engineering notes:
  - Under Armour is a Next.js app (server-side rendered with hydration).
  - Product data is embedded in ``__NEXT_DATA__`` JSON on every page.
  - Images are served from underarmour.com/content/ CDN paths.
  - Colour variants are listed in the Next.js page props under
    ``pageProps.product.variants`` or ``pageProps.initialProductData``.
  - The listing page uses a GraphQL / API pagination pattern accessible
    through ``?start=N&count=48`` query parameters.
"""

from __future__ import annotations

import json
from typing import Iterator

import scrapy

from manual_scraper_ext.base.ecommerce_spider import EcommerceSpider
from manual_scraper_ext.base.parser_utils import (
    extract_next_data,
    first_text,
    clean_price,
)
from manual_scraper_ext.base.image_utils import (
    is_product_image,
    clean_url,
    add_unique,
)


_CDN = ("underarmour.com/content/",)

_START_URLS = [
    "https://www.underarmour.com/en-us/c/mens/shoes/sandals-slides/",
]


class UnderarmourSpider(EcommerceSpider):
    """
    Crawls Under Armour for men's sandals and slides.

    Strategy
    --------
    1. Start on category pages; extract product URLs from ``__NEXT_DATA__``.
    2. Paginate using ``?start=N`` query parameters.
    3. On each product page, parse ``__NEXT_DATA__`` for all metadata and
       colour variants.  Fall back to JSON-LD if Next.js data is absent.
    """

    name = "underarmour"
    brand = "Under Armour"
    category = "Sandals"
    gender = "Men"
    cdn_patterns = _CDN

    start_urls = _START_URLS

    custom_settings = {
        **EcommerceSpider.custom_settings,
        "DOWNLOAD_DELAY": 2.5,
        "CONCURRENT_REQUESTS": 1,
        "USER_AGENT": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
        "DEFAULT_REQUEST_HEADERS": {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
        # Accept 418 as a non-error so we can log it and move on
        "HTTPERROR_ALLOWED_CODES": [418, 403, 401],
    }

    # Pagination step
    _page_size = 48

    def parse(self, response) -> Iterator:
        """Parse UA category page — extract product links and paginate."""
        if response.status in (418, 403, 401):
            self.logger.warning(
                "[UA] Bot-challenge response %d on %s — skipping",
                response.status, response.url,
            )
            return

        next_data = extract_next_data(response)

        # Try extracting product links from __NEXT_DATA__
        products = self._next_data_deep(
            next_data, "props", "pageProps", "products"
        ) or []

        yielded = 0
        for product in products:
            url = product.get("pdpURL") or product.get("url") or ""
            if url:
                full_url = response.urljoin(url)
                yield scrapy.Request(full_url, callback=self.parse_product)
                yielded += 1

        # Fallback: HTML product card links
        if not yielded:
            for link in response.css(
                'a[href*="/p/"]::attr(href), '
                'a[class*="product-card"]::attr(href)'
            ).getall():
                yield response.follow(link, callback=self.parse_product)

        # Pagination: ?start=N&count=48
        import urllib.parse as up
        parsed = up.urlparse(response.url)
        qs = up.parse_qs(parsed.query)
        current_start = int((qs.get("start") or ["0"])[0])

        if products or yielded:
            next_start = current_start + self._page_size
            if next_start < 500:  # safety ceiling
                qs["start"] = [str(next_start)]
                qs["count"] = [str(self._page_size)]
                next_url = up.urlunparse(
                    parsed._replace(query=up.urlencode(qs, doseq=True))
                )
                yield scrapy.Request(next_url, callback=self.parse)

    def parse_product(self, response) -> Iterator:
        """Parse a UA product detail page using ``__NEXT_DATA__``."""
        if response.status in (418, 403, 401):
            self.logger.warning(
                "[UA] Bot-challenge response %d on %s — skipping",
                response.status, response.url,
            )
            return

        next_data = extract_next_data(response)

        product = (
            self._next_data_deep(next_data, "props", "pageProps", "product")
            or self._next_data_deep(next_data, "props", "pageProps", "initialProductData")
            or {}
        )

        # ── Title ─────────────────────────────────────────────────────────────
        title = first_text(
            product.get("name", ""),
            response.css("h1[class*='product-name']::text").get(),
            response.css("h1::text").get(),
            response.css('meta[property="og:title"]::attr(content)').get(),
        )
        if not title:
            self.logger.warning("No title on %s", response.url)
            return

        # ── SKU / Price ────────────────────────────────────────────────────────
        sku = str(product.get("masterId") or product.get("id") or "")
        raw_price = product.get("price") or {}
        if isinstance(raw_price, dict):
            raw_price = raw_price.get("sales", {}).get("formatted", "")
        price = clean_price(str(raw_price))

        avail = product.get("availability") or {}
        availability = (
            "In Stock"
            if str(avail.get("status", "")).lower() in ("in_stock", "instock", "available")
            else "Out of Stock"
        )

        # ── Colour variants ────────────────────────────────────────────────────
        variants = product.get("variants") or product.get("colors") or []
        color_variants = self._ua_color_variants(variants)
        if not color_variants:
            color_variants = [("Default", "", {})]

        # ── All gallery images ─────────────────────────────────────────────────
        all_images = self._ua_images(product, response)

        self.logger.info(
            "[UA] %s — %d images, %d colours on %s",
            title, len(all_images), len(color_variants), response.url,
        )

        for color, color_id, variant_dict in color_variants:
            # Colour-specific images first
            color_urls: list[str] = []
            color_seen: set[str] = set()

            color_imgs = self._ua_color_images(variant_dict, response)
            for img in color_imgs:
                add_unique(color_urls, img, color_seen)

            for img in all_images:
                add_unique(color_urls, img, color_seen)

            yield self.make_item(
                product_name=title,
                product_url=response.url,
                image_urls=color_urls,
                color=color,
                sku=sku,
                variant=color_id,
                price=price,
                availability=availability,
            )

    # ─────────────────────────────────────────────────────────────────────────
    # UA-specific helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _ua_color_variants(
        self, variants: list
    ) -> list[tuple[str, str, dict]]:
        """Deduplicate colour variants from UA product data."""
        seen: set[str] = set()
        result: list[tuple[str, str, dict]] = []
        for v in variants:
            color = (
                v.get("color")
                or v.get("colorName")
                or v.get("displayName")
                or ""
            ).strip()
            vid = str(v.get("id") or v.get("colorHex") or "")
            if color and color.lower() not in seen:
                seen.add(color.lower())
                result.append((color, vid, v))
        return result

    def _ua_images(self, product: dict, response) -> list[str]:
        """Extract all gallery images from UA product JSON data."""
        seen: set[str] = set()
        urls: list[str] = []

        image_groups = product.get("imageGroups") or product.get("images") or []
        for group in image_groups:
            if isinstance(group, dict):
                imgs = group.get("images") or []
                for img in imgs:
                    src = img.get("link") or img.get("url") or img.get("src") or ""
                    if src and is_product_image(src, _CDN):
                        add_unique(urls, clean_url(response.urljoin(src)), seen)
            elif isinstance(group, str):
                if is_product_image(group, _CDN):
                    add_unique(urls, clean_url(group), seen)

        # Fallback: HTML gallery
        if not urls:
            urls = self.gallery_images(response)

        return urls

    def _ua_color_images(self, variant: dict, response) -> list[str]:
        """Return colour-specific images for a single variant."""
        seen: set[str] = set()
        urls: list[str] = []
        images = variant.get("images") or []
        for img in images:
            if isinstance(img, dict):
                src = img.get("link") or img.get("url") or img.get("src") or ""
            else:
                src = str(img)
            if src and is_product_image(src, _CDN):
                add_unique(urls, clean_url(response.urljoin(src)), seen)
        return urls
