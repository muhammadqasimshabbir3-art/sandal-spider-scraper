"""
ecommerce_spider.py
~~~~~~~~~~~~~~~~~~~
Intermediate base class for spiders targeting standard e-commerce websites.

Extends :class:`~manual_scraper_ext.base.base_spider.BaseSandalSpider` with:

- Shopify variant parsing (shared by many brands)
- JSON-LD product extraction
- Common pagination patterns (next-page link + ?page=N fallback)
- A ready-to-use ``parse_shopify_product`` that subclasses can call directly
  if the site is Shopify-based

Non-Shopify sites can still inherit this class for the JSON-LD and pagination
helpers while overriding only the site-specific parts.
"""

from __future__ import annotations

import json
import re
from typing import Iterator

import scrapy

from manual_scraper_ext.base.base_spider import BaseSandalSpider
from manual_scraper_ext.base.parser_utils import (
    extract_json_ld,
    get_json_ld_product,
    extract_next_data,
    first_text,
    clean_price,
)
from manual_scraper_ext.base.image_utils import (
    is_product_image,
    clean_url,
    add_unique,
)


class EcommerceSpider(BaseSandalSpider):
    """
    E-commerce base spider with Shopify, JSON-LD, and Next.js helpers.

    Subclasses inherit everything from :class:`BaseSandalSpider` and also
    get access to:

    - :meth:`parse_shopify_product` — full Shopify product page handler
    - :meth:`_shopify_variants` — extract variant list from Shopify JSON
    - :meth:`_unique_colors` — deduplicate variants by colour
    - :meth:`_jsonld_product_meta` — pull metadata from JSON-LD Product block
    - :meth:`_next_data_deep` — navigate __NEXT_DATA__ by dot-path
    - :meth:`standard_pagination` — next-link + ?page=N fallback pagination
    """

    # ─────────────────────────────────────────────────────────────────────────
    # Shopify helpers
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _is_color_option(value: str) -> bool:
        """Return True when the string looks like a colour name (not a size)."""
        return not re.match(r"^\d+(\.\d+)?$", value.strip())

    def _shopify_variants(self, response) -> list[dict]:
        """
        Extract the variant list from a Shopify product page.

        Tries (in order):
          1. ``<variant-radios>`` / ``<variant-selects>`` JSON script block
          2. ``var meta = { product: { variants: […] } }``
          3. ``/products/<handle>.js`` API (must be called separately)
          4. Colour radio ``<input>`` elements in HTML
          5. Single "Default" fallback
        """
        # ── Primary: Dawn-theme variant-radios JSON ──────────────────────────
        json_text = response.css(
            "variant-radios script[type='application/json']::text, "
            "variant-selects script[type='application/json']::text"
        ).get()
        if json_text:
            try:
                data = json.loads(json_text)
                if isinstance(data, list) and data:
                    return data
            except (json.JSONDecodeError, ValueError):
                self.logger.warning(
                    "JSON decode error (variant-radios) on %s", response.url
                )

        # ── Fallback 1: var meta = {…} ──────────────────────────────────────
        script = response.xpath(
            "//script[contains(., 'var meta =')]/text()"
        ).get()
        if script:
            match = re.search(r"var meta = (\{.*?\});", script, re.S)
            if match:
                try:
                    meta = json.loads(match.group(1))
                    variants = meta.get("product", {}).get("variants", [])
                    if variants:
                        return variants
                except (json.JSONDecodeError, ValueError):
                    pass

        # ── Fallback 2: product JSON in page data ────────────────────────────
        for script in response.css("script::text").getall():
            if '"variants"' in script:
                match = re.search(r'"variants"\s*:\s*(\[.*?\])', script, re.S)
                if match:
                    try:
                        variants = json.loads(match.group(1))
                        if isinstance(variants, list) and variants:
                            return variants
                    except (json.JSONDecodeError, ValueError):
                        continue

        # ── Fallback 3: colour radio buttons ────────────────────────────────
        colors = response.css(
            'input[name="Color"]::attr(value), '
            'input[name="colour"]::attr(value), '
            'input[name="color"]::attr(value)'
        ).getall()
        if colors:
            return [
                {"public_title": c, "id": f"html_{i}", "featured_image": None}
                for i, c in enumerate(colors)
            ]

        return [{"public_title": "Default", "id": "default", "featured_image": None}]

    def _unique_colors(
        self, variants: list[dict]
    ) -> list[tuple[str, str | None, dict]]:
        """
        Return ``(color, variant_id, variant_dict)`` for the FIRST variant
        of each unique colour.

        Handles Shopify public_title formats::

            "41 / Green", "Green / 41", "Brown", "41"
        """
        seen: set[str] = set()
        result: list[tuple] = []

        for v in variants:
            public_title = (v.get("public_title") or "").strip()
            color = ""

            for opt_key in ("option1", "option2", "option3"):
                opt = (v.get(opt_key) or "").strip()
                if opt and self._is_color_option(opt):
                    color = opt
                    break

            if not color and " / " in public_title:
                for part in [p.strip() for p in public_title.split("/")]:
                    if self._is_color_option(part):
                        color = part
                        break

            if not color:
                color = public_title or "Default"

            key = color.lower()
            if key not in seen:
                seen.add(key)
                result.append((color, v.get("id"), v))

        return result

    def _shopify_color_image(self, variant: dict, response) -> str | None:
        """
        Return the variant's own featured_image URL (colour-specific), or None.
        """
        fi = variant.get("featured_image") or {}
        src = fi.get("src", "") if isinstance(fi, dict) else ""
        if src and is_product_image(src, self.cdn_patterns):
            return clean_url(response.urljoin(src))
        return None

    def parse_shopify_product(self, response) -> Iterator:
        """
        Full Shopify product page handler — ready to use as a callback.

        Extracts title, variants, and gallery images then yields one
        :class:`~manual_scraper_ext.items.SandalItem` per unique colour.

        Usage in a subclass::

            def parse(self, response):
                for link in response.css('a[href*="/products/"]::attr(href)'):
                    yield response.follow(link, self.parse_shopify_product)
        """
        # ── Title ─────────────────────────────────────────────────────────────
        title = first_text(
            response.css("h1.product__title::text").get(),
            response.css("h1::text").get(),
            response.css('meta[property="og:title"]::attr(content)').get(),
        )
        if " – " in title:
            title = title.split(" – ")[0].strip()
        if not title:
            self.logger.warning("No title on %s", response.url)
            return

        # ── Price ─────────────────────────────────────────────────────────────
        raw_price = first_text(
            response.css(".price__regular .price-item::text").get(),
            response.css(".price::text").get(),
            response.css('[class*="price"]::text').get(),
        )
        price = clean_price(raw_price)

        # ── Availability ───────────────────────────────────────────────────────
        availability = "In Stock"
        if response.css('[class*="sold-out"], [class*="unavailable"]').get():
            availability = "Out of Stock"

        # ── Gallery images ─────────────────────────────────────────────────────
        all_images = self.gallery_images(response)
        self.logger.info(
            "[%s] %d unique gallery images on %s",
            title, len(all_images), response.url,
        )

        # ── Variants → unique colours ──────────────────────────────────────────
        variants = self._shopify_variants(response)
        color_variants = self._unique_colors(variants)

        self.logger.info(
            "[%s] %d colour variant(s): %s",
            title,
            len(color_variants),
            [c for c, _, __ in color_variants],
        )

        # ── Yield one item per colour ──────────────────────────────────────────
        for color, variant_id, variant_dict in color_variants:
            color_urls: list[str] = []
            color_seen: set[str] = set()

            # Colour-specific featured image first
            featured = self._shopify_color_image(variant_dict, response)
            if featured:
                add_unique(color_urls, featured, color_seen)

            # All remaining gallery images
            for url in all_images:
                add_unique(color_urls, url, color_seen)

            sku = str(variant_dict.get("sku") or variant_dict.get("id") or "")

            yield self.make_item(
                product_name=title,
                product_url=f"{response.url}?variant={variant_id}",
                image_urls=color_urls,
                color=color,
                sku=sku,
                variant=str(variant_id or ""),
                price=price,
                availability=availability,
            )

    # ─────────────────────────────────────────────────────────────────────────
    # JSON-LD helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _jsonld_product_meta(self, response) -> dict:
        """
        Return a normalised metadata dict from the page's JSON-LD Product block.

        Keys: brand, product_name, sku, price, availability, description
        All values are strings (empty string when not found).
        """
        block = get_json_ld_product(response)
        if not block:
            return {}

        offers = block.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}

        availability_url = offers.get("availability", "")
        availability = (
            "In Stock"
            if "InStock" in availability_url
            else ("Out of Stock" if availability_url else "")
        )

        brand = block.get("brand") or {}
        brand_name = (
            brand.get("name", "") if isinstance(brand, dict) else str(brand)
        )

        return {
            "brand":        brand_name or self.brand,
            "product_name": block.get("name", ""),
            "sku":          block.get("sku", ""),
            "price":        clean_price(str(offers.get("price", ""))),
            "availability": availability,
            "description":  block.get("description", ""),
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Next.js helpers
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _next_data_deep(data: dict, *keys: str):
        """
        Navigate a nested dict using a sequence of *keys*.

        Returns ``None`` at the first missing key.

        Example::

            value = self._next_data_deep(data, "props", "pageProps", "product")
        """
        for key in keys:
            if not isinstance(data, dict):
                return None
            data = data.get(key)
        return data

    # ─────────────────────────────────────────────────────────────────────────
    # Pagination
    # ─────────────────────────────────────────────────────────────────────────

    def standard_pagination(
        self,
        response,
        *,
        callback=None,
        max_pages: int = 50,
    ) -> Iterator[scrapy.Request]:
        """
        Generic pagination handler.

        Tries common "next page" link patterns then falls back to ``?page=N``.

        Parameters
        ----------
        response:
            Current collection/listing response.
        callback:
            Callback for the next page request.  Defaults to ``self.parse``.
        max_pages:
            Safety ceiling to prevent infinite pagination.
        """
        cb = callback or self.parse

        # ── Next-page link patterns ────────────────────────────────────────────
        next_href = (
            response.xpath('//a[@aria-label="Next page"]/@href').get()
            or response.css("a.next::attr(href)").get()
            or response.css('a[rel="next"]::attr(href)').get()
            or response.css('.pagination__next::attr(href)').get()
            or response.css('[class*="next-page"]::attr(href)').get()
            or response.css('[class*="pagination"] a[aria-label*="ext"]::attr(href)').get()
        )
        if next_href:
            yield response.follow(next_href, callback=cb)
            return

        # ── ?page=N fallback ──────────────────────────────────────────────────
        import urllib.parse as _up
        parsed = _up.urlparse(response.url)
        qs = _up.parse_qs(parsed.query)
        current_page = int((qs.get("page") or ["1"])[0])
        if current_page < max_pages:
            next_page = current_page + 1
            qs["page"] = [str(next_page)]
            next_url = _up.urlunparse(
                parsed._replace(query=_up.urlencode(qs, doseq=True))
            )
            yield scrapy.Request(next_url, callback=cb)
