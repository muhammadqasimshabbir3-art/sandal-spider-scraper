"""
zappos.py
~~~~~~~~~
Spider for https://www.zappos.com/ — Men's sandals and slides.

Reverse-engineering notes:
  - Zappos (Amazon subsidiary) is a React/Redux app.
  - Product data is embedded in ``window.__INITIAL_STATE__`` as a large JSON
    object within a ``<script>`` tag.
  - The listing page API is:
      GET https://www.zappos.com/search?term=mens+sandals&filters=gender%3AMen
  - Product detail pages contain ``window.__INITIAL_STATE__`` with product
    metadata, all colour variants, and image URLs.
  - Images are hosted on m.media-amazon.com or zappos CDN.
  - Pagination uses ``?p=N`` query parameter.
"""

from __future__ import annotations

import json
import re
from typing import Iterator

import scrapy

from manual_scraper_ext.base.ecommerce_spider import EcommerceSpider
from manual_scraper_ext.base.parser_utils import first_text, clean_price
from manual_scraper_ext.base.image_utils import (
    is_product_image,
    clean_url,
    add_unique,
)


_CDN = ("m.media-amazon.com", "zappos.com/images", "images.zappos.com")

_START_URLS = [
    "https://www.zappos.com/men-sandals/CK_XARC51wHAAQLiAgMBAhg.zso",
]


class ZapposSpider(EcommerceSpider):
    """
    Crawls Zappos.com for men's sandals and slides.

    Strategy
    --------
    1. Start on category pages and extract product links from HTML.
    2. Paginate via ``?p=N``.
    3. On product pages, parse ``window.__INITIAL_STATE__`` for all data.
    4. Fall back to JSON-LD if state data is not found.
    """

    name = "zappos"
    brand = "Zappos"
    category = "Sandals"
    gender = "Men"
    cdn_patterns = _CDN

    start_urls = _START_URLS

    custom_settings = {
        **EcommerceSpider.custom_settings,
        "DOWNLOAD_DELAY": 2.0,
        "CONCURRENT_REQUESTS": 1,
    }

    def parse(self, response) -> Iterator:
        """Parse Zappos category / search results page."""
        # Product links from listing
        product_links = response.css(
            'a[href*="/product/"]::attr(href), '
            'article a::attr(href)'
        ).getall()

        seen: set[str] = set()
        for link in product_links:
            if link not in seen and "/product/" in link:
                seen.add(link)
                yield response.follow(link, callback=self.parse_product)

        # Pagination
        yield from self.standard_pagination(response, max_pages=30)

    def parse_product(self, response) -> Iterator:
        """Parse a Zappos product detail page using window.__INITIAL_STATE__."""
        # ── window.__INITIAL_STATE__ ───────────────────────────────────────────
        product = self._zappos_initial_state(response)

        # ── JSON-LD fallback ───────────────────────────────────────────────────
        ld_meta = self._jsonld_product_meta(response)

        title = first_text(
            (product or {}).get("name", ""),
            ld_meta.get("product_name", ""),
            response.css("h1[class*='product']::text").get(),
            response.css("h1::text").get(),
            response.css('meta[property="og:title"]::attr(content)').get(),
        )
        if not title:
            self.logger.warning("No title on %s", response.url)
            return

        brand = first_text(
            (product or {}).get("brandName", ""),
            ld_meta.get("brand", ""),
        ) or self.brand

        sku = str((product or {}).get("styleId") or ld_meta.get("sku") or "")
        price = clean_price(
            str((product or {}).get("price", "")) or ld_meta.get("price", "")
        )
        availability = (
            "In Stock"
            if (product or {}).get("inStock", True)
            else "Out of Stock"
        )

        # ── Gallery images ─────────────────────────────────────────────────────
        all_images = self._zappos_images(product, response)

        # ── Colour variants ────────────────────────────────────────────────────
        colors = self._zappos_colors(product, response)
        if not colors:
            colors = [("Default", "", {})]

        self.logger.info(
            "[Zappos] %s — %d images, %d colours on %s",
            title, len(all_images), len(colors), response.url,
        )

        for color, color_id, variant_dict in colors:
            color_urls: list[str] = []
            color_seen: set[str] = set()

            for img in self._zappos_color_images(variant_dict, response):
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
    # Zappos-specific helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _zappos_initial_state(self, response) -> dict | None:
        """Extract ``window.__INITIAL_STATE__`` from the page."""
        for script in response.css("script::text").getall():
            if "__INITIAL_STATE__" in script:
                match = re.search(
                    r"__INITIAL_STATE__\s*=\s*(\{.*)", script, re.S
                )
                if match:
                    try:
                        data = json.loads(match.group(1).rstrip(";"))
                        # Zappos stores product info in various locations
                        return (
                            data.get("product")
                            or data.get("productDetail", {}).get("product")
                            or {}
                        )
                    except (json.JSONDecodeError, ValueError):
                        pass
        return None

    def _zappos_images(self, product: dict | None, response) -> list[str]:
        """Extract gallery images from Zappos product state."""
        seen: set[str] = set()
        urls: list[str] = []

        if product:
            images = product.get("images") or product.get("productImages") or []
            for img in images:
                if isinstance(img, dict):
                    src = (
                        img.get("imageUrl")
                        or img.get("url")
                        or img.get("src")
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

    def _zappos_colors(
        self, product: dict | None, response
    ) -> list[tuple[str, str, dict]]:
        """Extract colour variants from Zappos product state."""
        seen: set[str] = set()
        result: list[tuple[str, str, dict]] = []

        if product:
            for key in ("colors", "colorOptions", "variants"):
                items = product.get(key) or []
                for v in items:
                    if not isinstance(v, dict):
                        continue
                    name = (
                        v.get("colorName")
                        or v.get("color")
                        or v.get("name")
                        or ""
                    ).strip()
                    cid = str(v.get("colorId") or v.get("id") or "")
                    if name and name.lower() not in seen:
                        seen.add(name.lower())
                        result.append((name, cid, v))

        return result

    def _zappos_color_images(self, variant: dict, response) -> list[str]:
        """Return colour-specific images for one Zappos colour variant."""
        seen: set[str] = set()
        urls: list[str] = []

        for img in variant.get("images", []):
            if isinstance(img, dict):
                src = img.get("imageUrl") or img.get("url") or img.get("src") or ""
            else:
                src = str(img)
            if src and is_product_image(src, _CDN):
                add_unique(urls, clean_url(response.urljoin(src)), seen)

        return urls
