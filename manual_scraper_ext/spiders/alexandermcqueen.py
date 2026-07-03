"""
alexandermcqueen.py
~~~~~~~~~~~~~~~~~~~
Spider for https://www.alexandermcqueen.com/ — Men's sandals.

Reverse-engineering notes:
  - Alexander McQueen runs on Salesforce Commerce Cloud (SFCC) with a
    custom JavaScript frontend.
  - Product listing pages are server-rendered HTML.  Product links follow
    the pattern /en-us/sandals/product-name/SKU.html.
  - Product detail pages include JSON-LD ``Product`` and also embed product
    data in ``window.pageData`` or ``window.utag_data``.
  - Images are served from images.alexandermcqueen.com (Akamai CDN).
  - Colour variants appear in HTML colour-swatch elements with
    ``data-colorname`` attributes and in the JSON-LD ``offers`` array.
  - Pagination uses ``?start=N&sz=12``.
"""

from __future__ import annotations

import json
import re
from typing import Iterator

import scrapy

from manual_scraper_ext.base.ecommerce_spider import EcommerceSpider
from manual_scraper_ext.base.parser_utils import (
    extract_json_ld,
    first_text,
    clean_price,
)
from manual_scraper_ext.base.image_utils import (
    is_product_image,
    clean_url,
    add_unique,
)


_CDN = ("images.alexandermcqueen.com", "alexandermcqueen.com/product")

_START_URLS = [
    "https://www.alexandermcqueen.com/en-nl/men/shoes/sandals",
]


class AlexanderMcQueenSpider(EcommerceSpider):
    """
    Crawls Alexander McQueen for men's sandals.

    The gender filter is applied by inspecting product metadata; the site's
    sandal category landing page may include unisex or women's products
    which are skipped via ``_is_mens``.
    """

    name = "alexandermcqueen"
    brand = "Alexander McQueen"
    category = "Sandals"
    gender = "Men"
    cdn_patterns = _CDN

    start_urls = _START_URLS

    custom_settings = {
        **EcommerceSpider.custom_settings,
        "DOWNLOAD_DELAY": 2.0,
        "CONCURRENT_REQUESTS": 1,
    }

    _page_size = 12

    def parse(self, response) -> Iterator:
        """Parse AMQ category / listing page."""
        product_links = response.css(
            'a[href*="/sandals/"]::attr(href), '
            'a[class*="product-card"]::attr(href), '
            '.product-list a::attr(href)'
        ).re(r'[^\'"]+\.html')

        seen: set[str] = set()
        for link in product_links:
            if link not in seen:
                seen.add(link)
                yield response.follow(link, callback=self.parse_product)

        # Pagination
        import urllib.parse as up
        parsed = up.urlparse(response.url)
        qs = up.parse_qs(parsed.query)
        start = int((qs.get("start") or ["0"])[0])

        if product_links:
            next_start = start + self._page_size
            if next_start < 300:
                qs["start"] = [str(next_start)]
                next_url = up.urlunparse(
                    parsed._replace(query=up.urlencode(qs, doseq=True))
                )
                yield scrapy.Request(next_url, callback=self.parse)

    def parse_product(self, response) -> Iterator:
        """Parse an AMQ product detail page."""
        # ── Gender filter ──────────────────────────────────────────────────────
        if not self._is_mens(response):
            self.logger.debug("Skipping non-mens product: %s", response.url)
            return

        # ── JSON-LD ────────────────────────────────────────────────────────────
        ld_meta = self._jsonld_product_meta(response)

        title = first_text(
            ld_meta.get("product_name", ""),
            response.css("h1[class*='product-name']::text").get(),
            response.css("h1::text").get(),
            response.css('meta[property="og:title"]::attr(content)').get(),
        )
        if not title:
            self.logger.warning("No title on %s", response.url)
            return

        sku = ld_meta.get("sku") or self._amq_sku(response)
        price = ld_meta.get("price") or ""
        availability = ld_meta.get("availability") or "In Stock"

        # ── Gallery images ─────────────────────────────────────────────────────
        all_images = self._amq_images(response)

        # ── Colour variants ────────────────────────────────────────────────────
        colors = self._amq_colors(response)
        if not colors:
            colors = [("Default", "", {})]

        self.logger.info(
            "[AMQ] %s — %d images, %d colours on %s",
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

    # ── AMQ helpers ──────────────────────────────────────────────────────────

    def _is_mens(self, response) -> bool:
        """Return True when the page is clearly a men's product."""
        breadcrumb = " ".join(
            response.css('[class*="breadcrumb"] ::text').getall()
        ).lower()
        if "men" in breadcrumb or "man" in breadcrumb:
            return True
        gender_meta = response.css(
            'meta[name="gender"]::attr(content), '
            'meta[property="product:gender"]::attr(content)'
        ).get() or ""
        if "men" in gender_meta.lower():
            return True
        # If we can't determine gender, accept the product
        if not any(w in breadcrumb for w in ("women", "woman", "girl", "kids", "child")):
            return True
        return False

    def _amq_sku(self, response) -> str:
        """Extract SKU from URL or page data."""
        match = re.search(r'/([A-Z0-9]{6,})\.html', response.url)
        return match.group(1) if match else ""

    def _amq_images(self, response) -> list[str]:
        """Extract gallery images from AMQ product page."""
        seen: set[str] = set()
        urls: list[str] = []

        # Product gallery thumbnails and zoomed images
        for img in response.css(
            '.pdp-gallery img, '
            '.product-gallery img, '
            '[class*="product-image"] img'
        ):
            for attr in ("src", "data-src", "data-zoom-src", "data-large"):
                src = img.attrib.get(attr, "")
                if src and is_product_image(src, _CDN):
                    add_unique(urls, clean_url(response.urljoin(src)), seen)

        if not urls:
            urls = self.gallery_images(response)

        return urls

    def _amq_colors(self, response) -> list[tuple[str, str, dict]]:
        """Extract colour swatches from AMQ product page."""
        seen: set[str] = set()
        result: list[tuple[str, str, dict]] = []

        for swatch in response.css(
            '[data-colorname], '
            '[class*="color-swatch"], '
            '[class*="colour-swatch"]'
        ):
            name = (
                swatch.attrib.get("data-colorname")
                or swatch.attrib.get("aria-label")
                or swatch.attrib.get("title")
                or ""
            ).strip()
            cid = swatch.attrib.get("data-color-id") or swatch.attrib.get("data-value") or ""
            if name and name.lower() not in seen:
                seen.add(name.lower())
                result.append((name, cid, {}))

        return result
