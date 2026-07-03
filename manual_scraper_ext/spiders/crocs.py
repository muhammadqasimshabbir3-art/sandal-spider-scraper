"""
crocs.py
~~~~~~~~
Spider for https://www.crocs.com/ — Men's sandals, slides, and flip-flops.

Reverse-engineering notes:
  - Crocs runs on Salesforce Commerce Cloud (SFCC) with a Next.js frontend.
  - Product data is embedded in ``__NEXT_DATA__`` under:
      props.pageProps.product  (product detail page)
  - Images are served from media.crocs.com.
  - Colour variants are found in ``product.variationAttributes`` (SFCC format).
  - Pagination uses ``?start=N&sz=24``.
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


_CDN = ("media.crocs.com", "crocs.com/media", "crocs.scene7.com")

_START_URLS = [
    "https://www.crocs.com/c/men/footwear/sandals",
]


class CrocsSpider(EcommerceSpider):
    """
    Crawls Crocs.com for men's sandals, slides, and flip-flops.

    Strategy
    --------
    1. Listing pages: extract product links from ``__NEXT_DATA__`` or HTML.
    2. Paginate via ``?start=N&sz=24``.
    3. Product pages: parse ``__NEXT_DATA__`` for SFCC product data.
    4. Extract ``variationAttributes`` for colour variants and images.
    """

    name = "crocs"
    brand = "Crocs"
    category = "Sandals"
    gender = "Men"
    cdn_patterns = _CDN

    start_urls = _START_URLS

    custom_settings = {
        **EcommerceSpider.custom_settings,
        "DOWNLOAD_DELAY": 1.5,
        "CONCURRENT_REQUESTS": 2,
    }

    _page_size = 24

    def parse(self, response) -> Iterator:
        """Parse Crocs category / listing page."""
        next_data = extract_next_data(response)

        products = (
            self._next_data_deep(next_data, "props", "pageProps", "products")
            or self._next_data_deep(
                next_data, "props", "pageProps", "searchResults", "hits"
            )
            or []
        )

        yielded = 0
        for product in products:
            url = product.get("url") or product.get("productUrl") or ""
            if url:
                yield response.follow(url, callback=self.parse_product)
                yielded += 1

        if not yielded:
            for link in response.css(
                'a[href*="/p/"]::attr(href), '
                '.product-grid-item a::attr(href)'
            ).getall():
                yield response.follow(link, callback=self.parse_product)
                yielded += 1

        import urllib.parse as up
        parsed = up.urlparse(response.url)
        qs = up.parse_qs(parsed.query)
        current_start = int((qs.get("start") or ["0"])[0])

        if yielded:
            next_start = current_start + self._page_size
            if next_start < 500:
                qs["start"] = [str(next_start)]
                qs["sz"] = [str(self._page_size)]
                next_url = up.urlunparse(
                    parsed._replace(query=up.urlencode(qs, doseq=True))
                )
                yield scrapy.Request(next_url, callback=self.parse)

    def parse_product(self, response) -> Iterator:
        """Parse a Crocs product detail page from __NEXT_DATA__."""
        next_data = extract_next_data(response)
        product = (
            self._next_data_deep(next_data, "props", "pageProps", "product") or {}
        )

        title = first_text(
            product.get("name", ""),
            response.css("h1::text").get(),
            response.css('meta[property="og:title"]::attr(content)').get(),
        )
        if not title:
            ld_meta = self._jsonld_product_meta(response)
            title = ld_meta.get("product_name", "")
        if not title:
            self.logger.warning("No title on %s", response.url)
            return

        sku = str(product.get("id") or product.get("masterId") or "")
        price_data = product.get("price") or {}
        raw_price = str(
            (price_data.get("sales") or {}).get("formatted", "")
            if isinstance(price_data, dict) else price_data
        )
        price = clean_price(raw_price)

        avail = product.get("availability") or {}
        availability = (
            "In Stock"
            if str(avail.get("status", "")).lower() in ("in_stock", "orderable")
            else "Out of Stock"
        )

        colors = self._sfcc_colors(product)
        if not colors:
            colors = [("Default", "", {})]

        all_images = self._crocs_images(product, response)

        self.logger.info(
            "[Crocs] %s — %d images, %d colours on %s",
            title, len(all_images), len(colors), response.url,
        )

        for color, color_id, variant_dict in colors:
            color_urls: list[str] = []
            color_seen: set[str] = set()

            for img in self._crocs_color_images(variant_dict, response):
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

    # ── SFCC / Crocs helpers ─────────────────────────────────────────────────

    def _sfcc_colors(self, product: dict) -> list[tuple[str, str, dict]]:
        seen: set[str] = set()
        result: list[tuple[str, str, dict]] = []
        for attr in product.get("variationAttributes") or []:
            if attr.get("id", "").lower() not in ("color", "colour"):
                continue
            for val in attr.get("values") or []:
                name = (
                    val.get("displayValue") or val.get("value") or val.get("id") or ""
                ).strip()
                cid = str(val.get("id") or "")
                if name and name.lower() not in seen:
                    seen.add(name.lower())
                    result.append((name, cid, val))
        if not result:
            for v in product.get("variants") or []:
                name = (v.get("colorName") or v.get("color") or "").strip()
                cid = str(v.get("id") or "")
                if name and name.lower() not in seen:
                    seen.add(name.lower())
                    result.append((name, cid, v))
        return result

    def _crocs_images(self, product: dict, response) -> list[str]:
        seen: set[str] = set()
        urls: list[str] = []
        for key in ("images", "imageGroups", "media"):
            for img in product.get(key) or []:
                src = (
                    img.get("link") or img.get("url") or img.get("src") or ""
                    if isinstance(img, dict) else str(img)
                )
                if src and is_product_image(src, _CDN):
                    add_unique(urls, clean_url(response.urljoin(src)), seen)
        return urls or self.gallery_images(response)

    def _crocs_color_images(self, variant: dict, response) -> list[str]:
        seen: set[str] = set()
        urls: list[str] = []
        for img in variant.get("images") or []:
            src = (
                img.get("link") or img.get("url") or img.get("src") or ""
                if isinstance(img, dict) else str(img)
            )
            if src and is_product_image(src, _CDN):
                add_unique(urls, clean_url(response.urljoin(src)), seen)
        return urls
