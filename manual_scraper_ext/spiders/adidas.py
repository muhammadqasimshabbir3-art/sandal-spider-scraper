"""
adidas.py
~~~~~~~~~
Spider for https://www.adidas.com/ — Men's sandals and slides.

Reverse-engineering notes:
  - Adidas is a Next.js app.  Product data lives in ``__NEXT_DATA__`` under
    ``props.pageProps.componentProps.articleDetails`` (PDP pages).
  - The listing page exposes a JSON search API:
      GET https://www.adidas.com/api/products?gender=men&category=sandals
          &start=0&count=48&sort=newest
  - Images use the ``assets.adidas.com`` CDN.
  - Colour variants are in ``articleDetails.colorVariations``.
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


_CDN = ("assets.adidas.com", "assets1.adidas.com", "assets2.adidas.com")

_START_URLS = [
    "https://www.adidas.com/us/men-sandals",
    "https://www.adidas.com/us/men-slides",
]


class AdidasSpider(EcommerceSpider):
    """
    Crawls Adidas.com for men's sandals and slides.

    Strategy
    --------
    1. Start on category pages; extract product links from ``__NEXT_DATA__``.
    2. Paginate via ``start`` offset in the internal search API query string.
    3. Product pages: parse ``__NEXT_DATA__`` for article details and variants.
    """

    name = "adidas"
    brand = "Adidas"
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
        """Parse Adidas category / listing page."""
        next_data = extract_next_data(response)

        # Adidas stores product grid items in componentProps.products or hits
        products = (
            self._next_data_deep(
                next_data, "props", "pageProps", "componentProps", "products"
            )
            or self._next_data_deep(
                next_data, "props", "pageProps", "searchResult", "items"
            )
            or []
        )

        yielded = 0
        for product in products:
            url = (
                product.get("url")
                or product.get("altSports", {}).get("url")
                or ""
            )
            if url:
                yield response.follow(url, callback=self.parse_product)
                yielded += 1

        if not yielded:
            for link in response.css(
                'a[href*="/product/"]::attr(href), '
                '.glass-product-card a::attr(href)'
            ).getall():
                yield response.follow(link, callback=self.parse_product)
                yielded += 1

        # Pagination
        import urllib.parse as up
        parsed = up.urlparse(response.url)
        qs = up.parse_qs(parsed.query)
        start = int((qs.get("start") or ["0"])[0])
        if yielded:
            next_start = start + self._page_size
            if next_start < 500:
                qs["start"] = [str(next_start)]
                next_url = up.urlunparse(
                    parsed._replace(query=up.urlencode(qs, doseq=True))
                )
                yield scrapy.Request(next_url, callback=self.parse)

    def parse_product(self, response) -> Iterator:
        """Parse an Adidas product detail page from __NEXT_DATA__."""
        next_data = extract_next_data(response)

        article = (
            self._next_data_deep(
                next_data, "props", "pageProps", "componentProps", "articleDetails"
            )
            or self._next_data_deep(
                next_data, "props", "pageProps", "productData"
            )
            or {}
        )

        title = first_text(
            article.get("name", ""),
            response.css("h1[class*='product-name']::text").get(),
            response.css("h1::text").get(),
            response.css('meta[property="og:title"]::attr(content)').get(),
        )
        if not title:
            self.logger.warning("No title on %s", response.url)
            return

        sku = str(article.get("id") or article.get("articleNumber") or "")
        price_info = article.get("priceData") or {}
        price = clean_price(str(price_info.get("sale") or price_info.get("regular") or ""))

        availability = (
            "In Stock"
            if article.get("availability", {}).get("status", "").lower()
            in ("in_stock", "available")
            else "Out of Stock"
        )

        colors = self._adidas_colors(article)
        if not colors:
            colors = [("Default", "", {})]

        all_images = self._adidas_images(article, response)

        self.logger.info(
            "[Adidas] %s — %d images, %d colours on %s",
            title, len(all_images), len(colors), response.url,
        )

        for color, color_id, variant_dict in colors:
            color_urls: list[str] = []
            color_seen: set[str] = set()

            for img in self._adidas_color_images(variant_dict, response):
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

    # ── Adidas helpers ───────────────────────────────────────────────────────

    def _adidas_colors(self, article: dict) -> list[tuple[str, str, dict]]:
        seen: set[str] = set()
        result: list[tuple[str, str, dict]] = []
        for v in article.get("colorVariations") or article.get("variants") or []:
            name = (v.get("colorName") or v.get("color") or v.get("name") or "").strip()
            cid = str(v.get("articleNumber") or v.get("id") or "")
            if name and name.lower() not in seen:
                seen.add(name.lower())
                result.append((name, cid, v))
        return result

    def _adidas_images(self, article: dict, response) -> list[str]:
        seen: set[str] = set()
        urls: list[str] = []
        for key in ("images", "media", "gallery"):
            for img in article.get(key) or []:
                src = (
                    img.get("url") or img.get("src") or img.get("link") or ""
                    if isinstance(img, dict) else str(img)
                )
                if src and is_product_image(src, _CDN):
                    add_unique(urls, clean_url(response.urljoin(src)), seen)
        return urls or self.gallery_images(response)

    def _adidas_color_images(self, variant: dict, response) -> list[str]:
        seen: set[str] = set()
        urls: list[str] = []
        for img in variant.get("images") or []:
            src = (
                img.get("url") or img.get("src") or ""
                if isinstance(img, dict) else str(img)
            )
            if src and is_product_image(src, _CDN):
                add_unique(urls, clean_url(response.urljoin(src)), seen)
        return urls
