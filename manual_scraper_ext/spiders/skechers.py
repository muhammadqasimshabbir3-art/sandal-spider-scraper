"""
skechers.py
~~~~~~~~~~~
Spider for https://www.skechers.com/ — Men's sandals, slides, and flip-flops.

Reverse-engineering notes:
  - Skechers runs on a proprietary platform (not Shopify).
  - Product listing pages use paginated JSON APIs:
      GET /api/products?gender=mens&category=sandals&page=N
  - Product pages embed structured product data in JSON-LD (``Product`` type).
  - Gallery images are served from img.skechers.com CDN.
  - Colour variants are encoded as separate ``?color=XXX`` query params and
    also listed inside the JSON-LD ``offers`` array.
  - We request the listing JSON API directly to avoid JS-rendered HTML.
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


# CDN pattern — Skechers product images can come from the legacy image CDN
# and the image service used in JSON-LD payloads.
_CDN = ("img.skechers.com", "images.skechers.com", "skechers.com")

# Men's sandal category API endpoints
# Note: Skechers uses SFRA (Salesforce Reference Architecture).
# The main category pages redirect/load dynamically; we use the search
# update-grid endpoint which returns server-rendered HTML fragments.
_START_URLS = [
    "https://www.skechers.com/men/shoes/sandals/",
    "https://www.skechers.com/search/?q=mens+slides",
]

# Product listing API — returns JSON with product list
_API_BASE = "https://www.skechers.com/on/demandware.store/Sites-skechers-us-Site/en_US/Search-UpdateGrid"


class SkechersSpider(EcommerceSpider):
    """
    Crawls Skechers.com for men's sandals and slides.

    Strategy
    --------
    1. Start on category pages; extract embedded product data from the
       page's ``__NEXT_DATA__`` or inline JSON.
    2. Follow each product URL and parse JSON-LD for metadata and gallery.
    3. Handle colour variants by iterating the ``offers`` array in JSON-LD.
    """

    name = "skechers"
    brand = "Skechers"
    category = "Sandals"
    gender = "Men"
    cdn_patterns = _CDN

    start_urls = _START_URLS

    custom_settings = {
        **EcommerceSpider.custom_settings,
        "DOWNLOAD_DELAY": 1.5,
        "CONCURRENT_REQUESTS": 2,
    }

    def parse(self, response) -> Iterator:
        """
        Parse a Skechers category/listing page.

        Extracts product URLs from the HTML listing and follows pagination.
        """
        # Product links — Skechers uses anchor tags with /product/ path
        product_links = response.css(
            'a[href*="/product/"]::attr(href), '
            'a.product-card__link::attr(href)'
        ).getall()

        # Fallback: any link to a skechers product path
        if not product_links:
            product_links = response.css(
                'a[href*="skechers.com/"]::attr(href)'
            ).re(r'["\']?(/[^"\']*?(?:sandal|slide|flip)[^"\']*?)["\']?')

        seen_links: set[str] = set()
        for link in product_links:
            if link not in seen_links:
                seen_links.add(link)
                yield response.follow(link, callback=self.parse_product)

        # Pagination
        yield from self.standard_pagination(response, max_pages=30)

    def parse_product(self, response) -> Iterator:
        """
        Parse a Skechers product detail page.

        Extracts metadata from JSON-LD and gallery images from the CDN.
        """
        # ── JSON-LD metadata ───────────────────────────────────────────────────
        ld_meta = self._jsonld_product_meta(response)

        title = first_text(
            ld_meta.get("product_name", ""),
            response.css("h1.product-name::text").get(),
            response.css("h1::text").get(),
            response.css('meta[property="og:title"]::attr(content)').get(),
        )
        if not title:
            self.logger.warning("No title on %s", response.url)
            return

        price = ld_meta.get("price", "")
        availability = ld_meta.get("availability", "In Stock")
        sku = ld_meta.get("sku", "")

        # ── Gallery images ─────────────────────────────────────────────────────
        all_images = self._extract_skechers_images(response)
        if not all_images:
            all_images = self.gallery_images(response)

        self.logger.info(
            "[Skechers] %s — %d images on %s",
            title, len(all_images), response.url,
        )

        # ── Colour variants ────────────────────────────────────────────────────
        colors = self._extract_skechers_colors(response)
        if not colors:
            colors = [("Default", "", {})]

        for color, color_id, _ in colors:
            yield self.make_item(
                product_name=title,
                product_url=response.url,
                image_urls=all_images,
                color=color,
                sku=sku,
                price=price,
                availability=availability,
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Skechers-specific helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _extract_skechers_images(self, response) -> list[str]:
        """
        Extract gallery images from Skechers product page.

        Skechers serves images from img.skechers.com and embeds them in:
          - ``<img data-src="…">`` lazy-loaded attributes
          - Inline ``<script>`` blocks containing product JSON
          - Standard ``<img src>`` in the product gallery div
        """
        seen: set[str] = set()
        urls: list[str] = []

        # Gallery container images
        for img in response.css(
            '.product-gallery img, '
            '.product-images img, '
            '[class*="product-media"] img'
        ):
            for attr in ("src", "data-src", "data-zoom-src"):
                src = img.attrib.get(attr, "")
                if src and is_product_image(src, _CDN):
                    add_unique(urls, clean_url(response.urljoin(src)), seen)

        # JSON-LD product images
        from manual_scraper_ext.base.parser_utils import extract_json_ld

        for block in extract_json_ld(response, "Product"):
            image_data = block.get("image") or []
            if isinstance(image_data, str):
                image_data = [image_data]
            if isinstance(image_data, dict):
                image_data = [image_data.get("url") or ""]

            for img_url in image_data:
                if not img_url:
                    continue
                absolute = response.urljoin(img_url)
                if absolute.startswith("https://images.skechers.com/image"):
                    add_unique(urls, clean_url(absolute), seen)
                elif is_product_image(absolute, _CDN):
                    add_unique(urls, clean_url(absolute), seen)

        # Inline JSON product data
        for script in response.css("script::text").getall():
            if "img.skechers.com" in script:
                import re
                for match in re.finditer(
                    r'https://img\.skechers\.com[^\s"\'\\>]+\.(?:jpg|jpeg|png|webp)',
                    script,
                    re.I,
                ):
                    src = match.group(0)
                    if is_product_image(src, _CDN):
                        add_unique(urls, clean_url(src), seen)

        return urls

    def _extract_skechers_colors(
        self, response
    ) -> list[tuple[str, str, dict]]:
        """
        Return ``(color_name, color_id, {})`` tuples from the product page.

        Skechers renders colour swatches as anchor tags or input elements.
        """
        colors: list[tuple[str, str, dict]] = []
        seen: set[str] = set()

        # Colour swatch buttons / anchors
        for swatch in response.css(
            '[class*="color-swatch"], '
            '[class*="colour-swatch"], '
            'input[name="color"], '
            'input[name="Color"]'
        ):
            name = (
                swatch.attrib.get("aria-label")
                or swatch.attrib.get("title")
                or swatch.attrib.get("value")
                or ""
            ).strip()
            cid = swatch.attrib.get("data-color-id") or swatch.attrib.get("value") or ""
            if name and name.lower() not in seen:
                seen.add(name.lower())
                colors.append((name, cid, {}))

        # JSON-LD offers may contain colour info
        if not colors:
            from manual_scraper_ext.base.parser_utils import extract_json_ld
            for block in extract_json_ld(response, "Product"):
                offers = block.get("offers") or []
                if not isinstance(offers, list):
                    offers = [offers]
                for offer in offers:
                    color = offer.get("color") or offer.get("name") or ""
                    cid = offer.get("sku") or ""
                    if color and color.lower() not in seen:
                        seen.add(color.lower())
                        colors.append((color, cid, {}))

        return colors or [("Default", "", {})]
