"""
columbia.py
~~~~~~~~~~~
Spider for https://www.columbia.com/ — Men's sandals and slides.

Reverse-engineering notes:
  - Columbia Sportswear runs on a proprietary e-commerce platform.
  - Product listing pages are server-rendered HTML with embedded JSON data
    in ``<script type="application/json">`` blocks.
  - Product detail pages embed JSON-LD ``Product`` and also inline product
    data as ``window.__INITIAL_STATE__`` or ``window.digitalData``.
  - Images are hosted on columbia.scene7.com or images.columbia.com.
  - Colour variants are listed in the product JSON as ``swatchColors``.
  - Pagination uses standard ``?start=N&sz=48`` query parameters.
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


_CDN = ("columbia.scene7.com", "images.columbia.com", "columbia.com/media")

_START_URLS = [
    "https://www.columbia.com/c/mens-sandals/",
    "https://www.columbia.com/c/mens-slides/",
]


class ColumbiaSpider(EcommerceSpider):
    """
    Crawls Columbia.com for men's sandals and slides.

    Strategy
    --------
    1. Category pages contain product cards with product URLs.
    2. Paginate via ``?start=N&sz=48``.
    3. Product pages embed JSON-LD and inline ``window.__INITIAL_STATE__``.
    4. Extract colour variants from inline product data; fall back to swatches.
    """

    name = "columbia"
    brand = "Columbia"
    category = "Sandals"
    gender = "Men"
    cdn_patterns = _CDN

    start_urls = _START_URLS

    custom_settings = {
        **EcommerceSpider.custom_settings,
        "DOWNLOAD_DELAY": 1.5,
        "CONCURRENT_REQUESTS": 2,
    }

    _page_size = 48

    def parse(self, response) -> Iterator:
        """Parse Columbia category page and paginate."""
        # Product card links
        product_links = response.css(
            'a[href*="/p/"]::attr(href), '
            'a[class*="product-tile"]::attr(href), '
            '.product-card a::attr(href)'
        ).getall()

        seen: set[str] = set()
        for link in product_links:
            if link not in seen:
                seen.add(link)
                yield response.follow(link, callback=self.parse_product)

        # Pagination
        import urllib.parse as up
        parsed = up.urlparse(response.url)
        qs = up.parse_qs(parsed.query)
        current_start = int((qs.get("start") or ["0"])[0])

        if product_links:
            next_start = current_start + self._page_size
            if next_start < 500:
                qs["start"] = [str(next_start)]
                qs["sz"] = [str(self._page_size)]
                next_url = up.urlunparse(
                    parsed._replace(query=up.urlencode(qs, doseq=True))
                )
                yield scrapy.Request(next_url, callback=self.parse)

    def parse_product(self, response) -> Iterator:
        """Parse a Columbia product detail page."""
        # ── Try window.__INITIAL_STATE__ first ────────────────────────────────
        product = self._columbia_initial_state(response)

        # ── JSON-LD fallback ───────────────────────────────────────────────────
        ld_meta = self._jsonld_product_meta(response)

        title = first_text(
            (product or {}).get("name", ""),
            ld_meta.get("product_name", ""),
            response.css("h1.product-name::text").get(),
            response.css("h1::text").get(),
            response.css('meta[property="og:title"]::attr(content)').get(),
        )
        if not title:
            self.logger.warning("No title on %s", response.url)
            return

        sku = (product or {}).get("id") or ld_meta.get("sku") or ""
        price = clean_price(
            str((product or {}).get("price", "")) or ld_meta.get("price", "")
        )
        availability = ld_meta.get("availability") or "In Stock"

        # ── Gallery images ─────────────────────────────────────────────────────
        all_images = self._columbia_images(product, response)

        # ── Colour variants ────────────────────────────────────────────────────
        colors = self._columbia_colors(product, response)
        if not colors:
            colors = [("Default", "", {})]

        self.logger.info(
            "[Columbia] %s — %d images, %d colours on %s",
            title, len(all_images), len(colors), response.url,
        )

        for color, color_id, _ in colors:
            yield self.make_item(
                product_name=title,
                product_url=response.url,
                image_urls=all_images,
                color=color,
                sku=str(sku),
                variant=color_id,
                price=price,
                availability=availability,
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Columbia-specific helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _columbia_initial_state(self, response) -> dict | None:
        """Extract ``window.__INITIAL_STATE__`` product data."""
        for script in response.css("script::text").getall():
            if "__INITIAL_STATE__" in script:
                match = re.search(
                    r"__INITIAL_STATE__\s*=\s*(\{.*)", script, re.S
                )
                if match:
                    try:
                        data = json.loads(match.group(1).rstrip(";"))
                        return (
                            data.get("product")
                            or data.get("pdp", {}).get("product")
                            or {}
                        )
                    except (json.JSONDecodeError, ValueError):
                        pass
        return None

    def _columbia_images(self, product: dict | None, response) -> list[str]:
        """Extract gallery images from Columbia product data or HTML."""
        seen: set[str] = set()
        urls: list[str] = []

        if product:
            images = (
                product.get("images")
                or product.get("imageGroups")
                or product.get("media")
                or []
            )
            for img in images:
                if isinstance(img, dict):
                    src = (
                        img.get("url")
                        or img.get("src")
                        or img.get("link")
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

    def _columbia_colors(
        self, product: dict | None, response
    ) -> list[tuple[str, str, dict]]:
        """Extract colour variants from Columbia product data."""
        seen: set[str] = set()
        result: list[tuple[str, str, dict]] = []

        if product:
            swatches = (
                product.get("swatchColors")
                or product.get("variants")
                or product.get("colors")
                or []
            )
            for swatch in swatches:
                if not isinstance(swatch, dict):
                    continue
                name = (
                    swatch.get("colorName")
                    or swatch.get("name")
                    or swatch.get("displayName")
                    or ""
                ).strip()
                cid = str(swatch.get("id") or swatch.get("colorCode") or "")
                if name and name.lower() not in seen:
                    seen.add(name.lower())
                    result.append((name, cid, swatch))

        # HTML fallback
        if not result:
            for swatch in response.css(
                '[class*="color-swatch"] input, '
                '[class*="colour-swatch"] input, '
                'input[name="color"]'
            ):
                name = (
                    swatch.attrib.get("aria-label")
                    or swatch.attrib.get("value")
                    or ""
                ).strip()
                cid = swatch.attrib.get("value") or ""
                if name and name.lower() not in seen:
                    seen.add(name.lower())
                    result.append((name, cid, {}))

        return result
