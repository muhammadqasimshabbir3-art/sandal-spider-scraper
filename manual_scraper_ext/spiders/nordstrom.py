"""
nordstrom.py
~~~~~~~~~~~~
Spider for https://www.nordstrom.com/ — Men's sandals and slides.

Reverse-engineering notes:
  - Nordstrom is a Next.js application.
  - The product listing page embeds product data in ``__NEXT_DATA__`` JSON.
  - Product detail pages also use ``__NEXT_DATA__`` containing the full
    product object under ``props.pageProps.product``.
  - Images are served from n.nordstrommedia.com or nordstromrack.com CDN.
  - Colour variants are encoded in the Next.js hydration data.
  - The listing uses an internal GraphQL endpoint for pagination which is
    accessible via HTML pagination links as a fallback.
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


_CDN = ("n.nordstrommedia.com", "nordstromimage.com")

_START_URLS = [
    "https://www.nordstrom.com/browse/men/shoes/sandals?breadcrumb=Home%2FMen%2FShoes%2FSandals%20%26%20Flip-Flops&origin=topnav",
]


class NordstromSpider(EcommerceSpider):
    """
    Crawls Nordstrom.com for men's sandals and slides.

    Strategy
    --------
    1. Category pages: extract product URLs from ``__NEXT_DATA__`` and HTML.
    2. Paginate via ``?page=N`` query parameter.
    3. Product pages: parse ``__NEXT_DATA__`` for metadata, variants, images.
    """

    name = "nordstrom"
    brand = "Nordstrom"
    category = "Sandals"
    gender = "Men"
    cdn_patterns = _CDN

    start_urls = _START_URLS

    custom_settings = {
        **EcommerceSpider.custom_settings,
        "DOWNLOAD_DELAY": 2.0,
        "CONCURRENT_REQUESTS": 1,   # Nordstrom is aggressive about rate-limiting
        "DOWNLOAD_HANDLERS": {
            "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
            "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
        },
        "PLAYWRIGHT_LAUNCH_OPTIONS": {
            "headless": True,
        },
        "PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT": 30000,
    }

    def start_requests(self) -> Iterator[scrapy.Request]:
        for url in self.start_urls:
            yield scrapy.Request(
                url,
                callback=self.parse,
                meta={
                    "playwright": True,
                    "playwright_page_methods": [
                        {"method": "wait_for_load_state", "args": ["networkidle"]},
                    ],
                },
            )

    def standard_pagination(
        self,
        response,
        *,
        callback=None,
        max_pages: int = 50,
    ) -> Iterator[scrapy.Request]:
        for req in super().standard_pagination(response, callback=callback, max_pages=max_pages):
            req.meta.update(response.request.meta)
            yield req

    def parse(self, response) -> Iterator:
        """Parse Nordstrom category page."""
        next_data = extract_next_data(response)

        # Extract products from __NEXT_DATA__
        products = (
            self._next_data_deep(next_data, "props", "pageProps", "products")
            or self._next_data_deep(next_data, "props", "pageProps", "searchResults", "products")
            or []
        )

        yielded = 0
        for product in products:
            url = product.get("productPageUrl") or product.get("url") or ""
            if url:
                yield response.follow(url, callback=self.parse_product)
                yielded += 1

        # HTML fallback: product card links
        if not yielded:
            for link in response.css(
                'a[href*="/s/"]::attr(href), '
                'a[class*="product-card"]::attr(href)'
            ).getall():
                if "/s/" in link:
                    yield response.follow(link, callback=self.parse_product)

        # Pagination
        yield from self.standard_pagination(response, max_pages=30)

    def parse_product(self, response) -> Iterator:
        """Parse a Nordstrom product detail page from __NEXT_DATA__."""
        next_data = extract_next_data(response)

        product = (
            self._next_data_deep(next_data, "props", "pageProps", "product")
            or self._next_data_deep(next_data, "props", "pageProps", "productDetail")
            or {}
        )

        # ── Title ──────────────────────────────────────────────────────────────
        title = first_text(
            product.get("name", ""),
            product.get("displayName", ""),
            response.css("h1[class*='product-name']::text").get(),
            response.css("h1::text").get(),
            response.css('meta[property="og:title"]::attr(content)').get(),
        )
        if not title:
            self.logger.warning("No title on %s", response.url)
            return

        # ── Brand ──────────────────────────────────────────────────────────────
        brand = first_text(
            product.get("brand", ""),
            product.get("brandName", ""),
        ) or self.brand

        # ── SKU / Price ────────────────────────────────────────────────────────
        sku = str(product.get("styleNumber") or product.get("id") or "")
        price_data = product.get("priceRange") or product.get("price") or {}
        if isinstance(price_data, dict):
            raw_price = (
                price_data.get("regular", {}).get("high", "")
                or price_data.get("sale", {}).get("high", "")
                or str(price_data)
            )
        else:
            raw_price = str(price_data)
        price = clean_price(raw_price)

        availability = (
            "In Stock"
            if product.get("isAvailable", True)
            else "Out of Stock"
        )

        # ── Colour variants ────────────────────────────────────────────────────
        colors = self._nordstrom_colors(product)
        if not colors:
            colors = [("Default", "", {})]

        # ── Gallery images ─────────────────────────────────────────────────────
        all_images = self._nordstrom_images(product, response)

        self.logger.info(
            "[Nordstrom] %s — %d images, %d colours on %s",
            title, len(all_images), len(colors), response.url,
        )

        for color, color_id, variant_dict in colors:
            color_urls: list[str] = []
            color_seen: set[str] = set()

            for img in self._nordstrom_color_images(variant_dict, response):
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
    # Nordstrom-specific helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _nordstrom_colors(
        self, product: dict
    ) -> list[tuple[str, str, dict]]:
        """Extract colour options from Nordstrom product dict."""
        seen: set[str] = set()
        result: list[tuple[str, str, dict]] = []

        for key in ("colorOptions", "colors", "variants", "skus"):
            items = product.get(key) or []
            for v in items:
                if not isinstance(v, dict):
                    continue
                name = (
                    v.get("color")
                    or v.get("colorName")
                    or v.get("displayColor")
                    or v.get("name")
                    or ""
                ).strip()
                cid = str(v.get("colorCode") or v.get("id") or "")
                if name and name.lower() not in seen:
                    seen.add(name.lower())
                    result.append((name, cid, v))

        return result

    def _nordstrom_images(self, product: dict, response) -> list[str]:
        """Extract all gallery images from Nordstrom product data."""
        seen: set[str] = set()
        urls: list[str] = []

        for key in ("images", "media", "productImages"):
            imgs = product.get(key) or []
            for img in imgs:
                if isinstance(img, dict):
                    src = (
                        img.get("url")
                        or img.get("src")
                        or img.get("squareLargeUrl")
                        or ""
                    )
                elif isinstance(img, str):
                    src = img
                else:
                    continue
                if src and is_product_image(src, _CDN):
                    add_unique(urls, clean_url(response.urljoin(src)), seen)

        if not urls:
            urls = self.gallery_images(response)

        return urls

    def _nordstrom_color_images(
        self, variant: dict, response
    ) -> list[str]:
        """Return colour-specific images for a single Nordstrom colour."""
        seen: set[str] = set()
        urls: list[str] = []

        for imgs in (
            variant.get("images", []),
            variant.get("media", []),
        ):
            for img in imgs:
                if isinstance(img, dict):
                    src = img.get("url") or img.get("src") or ""
                else:
                    src = str(img)
                if src and is_product_image(src, _CDN):
                    add_unique(urls, clean_url(response.urljoin(src)), seen)

        return urls
