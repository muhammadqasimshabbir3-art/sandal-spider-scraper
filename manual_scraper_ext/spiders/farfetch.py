"""
farfetch.py
~~~~~~~~~~~
Spider for https://www.farfetch.com/ — Men's sandals.

Reverse-engineering notes:
  - Farfetch is a React/Next.js application.
  - Product listing pages embed data in ``__NEXT_DATA__`` under
    ``props.pageProps.initialState.listings.items``.
  - Product detail pages embed data in ``__NEXT_DATA__`` under
    ``props.pageProps.initialState.pdp.product``.
  - Images are served from cdn-images.farfetch-contents.com.
  - Colour variants are in ``product.colors`` in the product detail JSON.
  - Pagination uses ``?page=N`` query parameter on listing pages.
"""

from __future__ import annotations

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


_CDN = ("cdn-images.farfetch-contents.com", "cdn-static.farfetch.net")

_START_URLS = [
    "https://www.farfetch.com/pk/shopping/men/shoes-2/items.aspx?category=136360",
]


class FarfetchSpider(EcommerceSpider):
    """
    Crawls Farfetch.com for men's sandals and slides.

    Strategy
    --------
    1. Listing pages: extract product links from ``__NEXT_DATA__`` listings.
    2. Paginate via ``?page=N``.
    3. Product pages: parse ``__NEXT_DATA__`` for brand, variants, and images.
    """

    name = "farfetch"
    brand = "Farfetch"
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
        """Parse Farfetch listing page."""
        next_data = extract_next_data(response)

        items = (
            self._next_data_deep(
                next_data,
                "props", "pageProps", "initialState", "listings", "items",
            )
            or self._next_data_deep(
                next_data, "props", "pageProps", "products"
            )
            or []
        )

        yielded = 0
        for item in items:
            url = item.get("url") or item.get("shortDescription", {}).get("url") or ""
            if url:
                yield response.follow(url, callback=self.parse_product)
                yielded += 1

        if not yielded:
            for link in response.css(
                'a[href*="/shopping/men/"]::attr(href)'
            ).re(r'/shopping/men/[^"\'?]+'):
                yield response.follow(link, callback=self.parse_product)

        # Pagination
        yield from self.standard_pagination(response, max_pages=30)

    def parse_product(self, response) -> Iterator:
        """Parse a Farfetch product detail page from __NEXT_DATA__."""
        next_data = extract_next_data(response)

        product = (
            self._next_data_deep(
                next_data,
                "props", "pageProps", "initialState", "pdp", "product",
            )
            or self._next_data_deep(
                next_data, "props", "pageProps", "productData"
            )
            or {}
        )

        # ── Title ──────────────────────────────────────────────────────────────
        title = first_text(
            product.get("name", ""),
            product.get("shortDescription", ""),
            response.css("h1[class*='product-name']::text").get(),
            response.css("h1::text").get(),
            response.css('meta[property="og:title"]::attr(content)').get(),
        )
        if not title:
            self.logger.warning("No title on %s", response.url)
            return

        # ── Brand override (Farfetch lists many brands) ────────────────────────
        brand_data = product.get("brand") or {}
        brand = (
            brand_data.get("name", "") if isinstance(brand_data, dict) else str(brand_data)
        ).strip() or self.brand

        # ── SKU / Price ────────────────────────────────────────────────────────
        sku = str(product.get("id") or product.get("styleId") or "")
        price_info = product.get("priceInfo") or product.get("price") or {}
        raw_price = str(
            price_info.get("finalPrice")
            or price_info.get("price")
            or ""
        )
        price = clean_price(raw_price)

        availability = (
            "In Stock"
            if product.get("isAvailable", True)
            else "Out of Stock"
        )

        # ── Colour variants ────────────────────────────────────────────────────
        colors = self._farfetch_colors(product)
        if not colors:
            colors = [("Default", "", {})]

        # ── Gallery images ─────────────────────────────────────────────────────
        all_images = self._farfetch_images(product, response)

        self.logger.info(
            "[Farfetch] %s (%s) — %d images, %d colours on %s",
            title, brand, len(all_images), len(colors), response.url,
        )

        for color, color_id, variant_dict in colors:
            color_urls: list[str] = []
            color_seen: set[str] = set()

            for img in self._farfetch_color_images(variant_dict, response):
                add_unique(color_urls, img, color_seen)
            for img in all_images:
                add_unique(color_urls, img, color_seen)

            item = self.make_item(
                product_name=title,
                product_url=response.url,
                image_urls=color_urls,
                color=color,
                sku=sku,
                variant=color_id,
                price=price,
                availability=availability,
            )
            # Store the actual brand in meta for accurate folder naming
            item["brand"] = brand
            item["meta"]["brand"] = brand
            yield item

    # ── Farfetch helpers ─────────────────────────────────────────────────────

    def _farfetch_colors(self, product: dict) -> list[tuple[str, str, dict]]:
        seen: set[str] = set()
        result: list[tuple[str, str, dict]] = []
        for v in product.get("colors") or product.get("variants") or []:
            if not isinstance(v, dict):
                continue
            name = (
                v.get("color") or v.get("name") or v.get("colorName") or ""
            ).strip()
            cid = str(v.get("id") or v.get("colorId") or "")
            if name and name.lower() not in seen:
                seen.add(name.lower())
                result.append((name, cid, v))
        return result

    def _farfetch_images(self, product: dict, response) -> list[str]:
        seen: set[str] = set()
        urls: list[str] = []
        for img in product.get("images") or product.get("media") or []:
            src = (
                img.get("url") or img.get("src") or img.get("link") or ""
                if isinstance(img, dict) else str(img)
            )
            if src and is_product_image(src, _CDN):
                add_unique(urls, clean_url(response.urljoin(src)), seen)
        return urls or self.gallery_images(response)

    def _farfetch_color_images(self, variant: dict, response) -> list[str]:
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
