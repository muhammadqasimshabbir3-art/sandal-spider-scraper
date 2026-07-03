"""
asos.py
~~~~~~~
Spider for https://www.asos.com/ — Men's sandals, slides, and flip-flops.

Reverse-engineering notes:
  - ASOS provides a public product API:
      GET https://api.asos.com/product/search/v2/categories/<catId>
          ?channel=desktop-web&country=US&currency=USD
          &lang=en-US&limit=72&offset=N&store=COM&q=sandals
  - Men's sandals category ID: 4209
  - Men's slides / flip-flops category ID: 4927
  - Product detail API:
      GET https://api.asos.com/product/catalogue/v3/products/<productId>
          ?store=COM&lang=en-US&currency=USD&sizeSchema=US
  - Images are served from images.asos-media.com.
  - JSON responses contain full colour variant and gallery data.
"""

from __future__ import annotations

import json
from typing import Iterator

import scrapy

from manual_scraper_ext.base.ecommerce_spider import EcommerceSpider
from manual_scraper_ext.base.parser_utils import first_text, clean_price
from manual_scraper_ext.base.image_utils import (
    is_product_image,
    clean_url,
    add_unique,
)


_CDN = ("images.asos-media.com",)

# ASOS product search API base
_SEARCH_API = (
    "https://api.asos.com/product/search/v2/categories/{cat_id}"
    "?channel=desktop-web&country=US&currency=USD"
    "&lang=en-US&limit=72&offset={offset}&store=COM"
)

# ASOS product detail API
_PRODUCT_API = (
    "https://api.asos.com/product/catalogue/v3/products/{product_id}"
    "?store=COM&lang=en-US&currency=USD&sizeSchema=US"
)

# Men's sandal category IDs on ASOS
_CATEGORY_IDS = {
    "sandals": 6593,
    "slides": 4927,
}

_ASOS_HEADERS = {
    "Accept": "application/json",
    "Referer": "https://www.asos.com/",
    "Origin": "https://www.asos.com",
    "asos-c-name": "asos-web-productpage",
    "asos-c-plat": "web",
}


class AsosSpider(EcommerceSpider):
    """
    Crawls ASOS for men's sandals, slides, and flip-flops.

    Strategy
    --------
    1. Call the ASOS product search API directly for each category.
    2. Paginate via ``offset`` parameter.
    3. For each product, call the product detail API to get full metadata,
       gallery images, and colour variants.
    """

    name = "asos"
    brand = "ASOS"
    category = "Sandals"
    gender = "Men"
    cdn_patterns = _CDN

    start_urls = []  # generated programmatically in start_requests

    custom_settings = {
        **EcommerceSpider.custom_settings,
        "DOWNLOAD_DELAY": 1.2,
        "CONCURRENT_REQUESTS": 2,
        "ROBOTSTXT_OBEY": False,
        "DEFAULT_REQUEST_HEADERS": {
            **EcommerceSpider.custom_settings.get("DEFAULT_REQUEST_HEADERS", {}),
            **_ASOS_HEADERS,
        },
    }

    async def start(self):
        """Kick off the search API for each category."""
        for cat_name, cat_id in _CATEGORY_IDS.items():
            url = _SEARCH_API.format(cat_id=cat_id, offset=0)
            yield scrapy.Request(
                url,
                callback=self.parse,
                cb_kwargs={"cat_id": cat_id, "cat_name": cat_name, "offset": 0},
                headers=_ASOS_HEADERS,
            )

    def parse(self, response, cat_id: int = 0, cat_name: str = "sandals",
              offset: int = 0) -> Iterator:
        """Parse ASOS search API response — queue product detail requests."""
        try:
            data = json.loads(response.text)
        except (json.JSONDecodeError, ValueError):
            self.logger.error("JSON parse error on search API: %s", response.url)
            return

        products = data.get("products") or []
        total = data.get("itemCount") or 0

        for product in products:
            product_id = product.get("id")
            if not product_id:
                continue
            api_url = _PRODUCT_API.format(product_id=product_id)
            yield scrapy.Request(
                api_url,
                callback=self.parse_product,
                cb_kwargs={"listing_data": product},
                headers=_ASOS_HEADERS,
            )

        # Paginate
        next_offset = offset + 72
        if products and next_offset < min(total, 1000):
            next_url = _SEARCH_API.format(cat_id=cat_id, offset=next_offset)
            yield scrapy.Request(
                next_url,
                callback=self.parse,
                cb_kwargs={"cat_id": cat_id, "cat_name": cat_name, "offset": next_offset},
                headers=_ASOS_HEADERS,
            )

    def parse_product(self, response, listing_data: dict = None) -> Iterator:
        """Parse ASOS product detail API response."""
        try:
            data = json.loads(response.text)
        except (json.JSONDecodeError, ValueError):
            self.logger.error("JSON parse error on product API: %s", response.url)
            return

        # ── Title ──────────────────────────────────────────────────────────────
        title = first_text(
            data.get("name", ""),
            (listing_data or {}).get("name", ""),
        )
        if not title:
            self.logger.warning("No title from product API %s", response.url)
            return

        # ── Brand override ─────────────────────────────────────────────────────
        brand = (
            data.get("brand") or ""
        ).strip() or self.brand

        # ── SKU / Price ────────────────────────────────────────────────────────
        sku = str(data.get("id") or "")
        price_data = data.get("price") or {}
        price = clean_price(str(price_data.get("current", {}).get("text", "")))

        is_in_stock = data.get("isInStock", True)
        availability = "In Stock" if is_in_stock else "Out of Stock"

        # ── Product URL ────────────────────────────────────────────────────────
        pdp_url = f"https://www.asos.com/-/prd/{sku}"

        # ── Colour variants ────────────────────────────────────────────────────
        variants = data.get("variants") or []
        colours = self._asos_colors(variants)
        if not colours:
            colours = [("Default", "", {})]

        # ── All gallery images ─────────────────────────────────────────────────
        all_images = self._asos_images(data, response)

        self.logger.info(
            "[ASOS] %s — %d images, %d colours",
            title, len(all_images), len(colours),
        )

        for color, color_id, variant_dict in colours:
            color_urls: list[str] = []
            color_seen: set[str] = set()

            # Colour-specific images first
            for img in self._asos_color_images(variant_dict, response):
                add_unique(color_urls, img, color_seen)

            for img in all_images:
                add_unique(color_urls, img, color_seen)

            yield self.make_item(
                product_name=title,
                product_url=f"{pdp_url}?colourWayId={color_id}" if color_id else pdp_url,
                image_urls=color_urls,
                color=color,
                sku=sku,
                variant=color_id,
                price=price,
                availability=availability,
            )

    # ─────────────────────────────────────────────────────────────────────────
    # ASOS-specific helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _asos_colors(
        self, variants: list
    ) -> list[tuple[str, str, dict]]:
        """Deduplicate colour variants from ASOS variant list."""
        seen: set[str] = set()
        result: list[tuple[str, str, dict]] = []

        for v in variants:
            color = (v.get("colour") or v.get("colourWayName") or "").strip()
            cid = str(v.get("colourWayId") or "")
            if color and color.lower() not in seen:
                seen.add(color.lower())
                result.append((color, cid, v))

        return result

    def _asos_images(self, data: dict, response) -> list[str]:
        """Extract all product gallery images from ASOS API data."""
        seen: set[str] = set()
        urls: list[str] = []

        media = data.get("media") or {}
        images = media.get("images") or []
        for img in images:
            if isinstance(img, dict):
                src = img.get("url") or img.get("src") or ""
                if src:
                    full = clean_url("https://" + src.lstrip("/"))
                    if is_product_image(full, _CDN):
                        add_unique(urls, full, seen)

        return urls

    def _asos_color_images(self, variant: dict, response) -> list[str]:
        """Return colour-specific images for one ASOS variant."""
        seen: set[str] = set()
        urls: list[str] = []

        images = variant.get("images") or []
        for img in images:
            if isinstance(img, dict):
                src = img.get("url") or img.get("src") or ""
            else:
                src = str(img)
            if src:
                full = clean_url("https://" + src.lstrip("/"))
                if is_product_image(full, _CDN):
                    add_unique(urls, full, seen)

        return urls
