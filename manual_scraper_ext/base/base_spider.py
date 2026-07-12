"""
base_spider.py
~~~~~~~~~~~~~~
Abstract base class for all sandal-dataset spiders.

Every concrete spider inherits from :class:`BaseSandalSpider` and gets:

- Standard ``custom_settings`` (politeness, retry, user-agent, cache)
- Shared helper methods (image collection, URL cleaning, logging)
- The canonical ``SandalItem`` as the output item class

Concrete spiders only need to implement:

    ``parse(response)``         — collection / listing page handler
    ``parse_product(response)`` — product page handler (yields SandalItem)
"""

from __future__ import annotations

from typing import Iterator

import scrapy

from manual_scraper_ext.items import SandalItem
from manual_scraper_ext.base.image_utils import (
    collect_images_from_response,
    clean_url,
    add_unique,
    base_filename,
    is_product_image,
)
from manual_scraper_ext.base.metadata import build_metadata


# ─────────────────────────────────────────────────────────────────────────────
# Shared Scrapy settings applied to every spider unless overridden
# ─────────────────────────────────────────────────────────────────────────────

_BASE_CUSTOM_SETTINGS: dict = {
    # Politeness
    "CONCURRENT_REQUESTS": 2,
    "CONCURRENT_REQUESTS_PER_DOMAIN": 2,
    "DOWNLOAD_DELAY": 1.0,
    "RANDOMIZE_DOWNLOAD_DELAY": True,
    "AUTOTHROTTLE_ENABLED": True,
    "AUTOTHROTTLE_START_DELAY": 1.0,
    "AUTOTHROTTLE_MAX_DELAY": 30.0,
    "AUTOTHROTTLE_TARGET_CONCURRENCY": 1.5,

    # Retry
    "RETRY_ENABLED": True,
    "RETRY_TIMES": 3,
    "RETRY_HTTP_CODES": [500, 502, 503, 504, 403, 408, 429],

    # Image pipeline
    "ITEM_PIPELINES": {
        "manual_scraper_ext.pipelines.DatasetImagesPipeline": 1,
    },
    "IMAGES_STORE": "dataset",
    "IMAGES_URLS_FIELD": "image_urls",
    "IMAGES_RESULT_FIELD": "images",
    "IMAGES_MIN_HEIGHT": 0,
    "IMAGES_MIN_WIDTH": 0,

    # HTTP cache (off by default — enable with -s HTTPCACHE_ENABLED=True)
    "HTTPCACHE_ENABLED": False,
    "HTTPCACHE_DIR": ".scrapy/httpcache",

    # Duplicate URL filter
    "DUPEFILTER_CLASS": "scrapy.dupefilters.RFPDupeFilter",

    # Browser-like request headers
    "USER_AGENT": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "DEFAULT_REQUEST_HEADERS": {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    },

    # Misc
    "COOKIES_ENABLED": True,
    "FEED_EXPORT_ENCODING": "utf-8",
}


class BaseSandalSpider(scrapy.Spider):
    """
    Abstract base spider.  Subclass this and implement ``parse`` and
    ``parse_product``.

    Class attributes to set in every subclass
    -----------------------------------------
    name : str
        Scrapy spider name (used on the CLI).
    brand : str
        Human-readable brand name stored in ``metadata.json``.
    category : str
        Product category string stored in ``metadata.json``.
        Default: ``"Sandals"``.
    gender : str
        Target gender.  Default: ``"Men"``.
    cdn_patterns : tuple[str, ...]
        CDN URL substrings that must appear in a valid product image URL.
        Leave empty to accept any raster image.
    """

    # ── Spider identity ───────────────────────────────────────────────────────
    brand: str = ""
    category: str = "Sandals"
    gender: str = "Men"
    cdn_patterns: tuple[str, ...] = ()

    # ── Scrapy settings ───────────────────────────────────────────────────────
    custom_settings: dict = dict(_BASE_CUSTOM_SETTINGS)

    # ─────────────────────────────────────────────────────────────────────────
    # Required interface — subclasses must implement these
    # ─────────────────────────────────────────────────────────────────────────

    def parse(self, response):
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement parse()"
        )

    def parse_product(self, response):
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement parse_product()"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Image helpers
    # ─────────────────────────────────────────────────────────────────────────

    def gallery_images(self, response) -> list[str]:
        """
        Collect all unique product image URLs from *response* using the
        universal HTML image collector.

        Subclasses may override this to add site-specific extraction logic
        (e.g. parsing JSON-LD or API responses) before calling ``super()``.
        """
        return collect_images_from_response(
            response,
            cdn_patterns=self.cdn_patterns,
            logger=self.logger,
        )

    def clean(self, url: str) -> str:
        """Convert protocol-relative URL and strip resize parameters."""
        return clean_url(url)

    def add_image(self, lst: list[str], url: str, seen: set[str]) -> None:
        """Add *url* to *lst* if not already in *seen* (by base filename)."""
        add_unique(lst, url, seen)

    def is_product_image(self, url: str) -> bool:
        """Delegate to the shared image filter."""
        return is_product_image(url, self.cdn_patterns)

    # ─────────────────────────────────────────────────────────────────────────
    # Item builder
    # ─────────────────────────────────────────────────────────────────────────

    def make_item(
        self,
        *,
        product_name: str,
        product_url: str,
        image_urls: list[str],
        color: str = "",
        sku: str = "",
        variant: str = "",
        price: str = "",
        availability: str = "",
        category: str | None = None,
    ) -> SandalItem:
        """
        Construct a :class:`~manual_scraper_ext.items.SandalItem` using the
        canonical metadata schema.

        Parameters that are ``None`` default to the spider's class attributes.
        """
        meta = build_metadata(
            brand=self.brand,
            product_name=product_name,
            source=self.name,
            gender=self.gender,
            category=category if category is not None else self.category,
            sku=sku,
            variant=variant,
            color=color,
            price=price,
            availability=availability,
            product_url=product_url,
            images=[],          # filled by the pipeline
        )

        item = SandalItem()
        item["brand"]        = self.brand
        item["product_name"] = product_name
        item["color"]        = color
        item["source"]       = self.name
        item["meta"]         = meta
        item["image_urls"]   = image_urls
        item["images"]       = []
        return item

    # ─────────────────────────────────────────────────────────────────────────
    # Spider lifecycle
    # ─────────────────────────────────────────────────────────────────────────

    def start_requests(self):
        """
        Check if spider is in exclusion list before starting requests.
        
        Allows skipping specific spiders via EXCLUDED_SPIDERS setting or
        skip_spiders argument.
        """
        # Get exclusion list from settings and/or spider arguments
        excluded_list = self.settings.getlist("EXCLUDED_SPIDERS", [])
        
        # Also check if skip_spiders was passed as a spider argument
        # Usage: scrapy crawl all -a skip_spiders="selle-sandals,zappos"
        skip_spiders_arg = getattr(self, "skip_spiders", None)
        if skip_spiders_arg:
            excluded_list.extend([s.strip() for s in skip_spiders_arg.split(",")])
        
        if self.name in excluded_list:
            self.logger.warning(
                "[%s] Spider is in EXCLUDED_SPIDERS list. Skipping.",
                self.name,
            )
            return  # No requests yielded — spider will close immediately
        
        # Call parent's start_requests (which yields initial requests)
        # If not overridden by a subclass, Scrapy will use the default behavior
        for request in super().start_requests():
            yield request

    # ─────────────────────────────────────────────────────────────────────────
    # Pagination helpers
    # ─────────────────────────────────────────────────────────────────────────

    def follow_next_page(
        self,
        response,
        *selectors: str,
        callback=None,
    ) -> Iterator[scrapy.Request]:
        """
        Yield a request for the next pagination page.

        Tries each CSS/XPath selector in *selectors* in order and follows the
        first href found.

        Parameters
        ----------
        response:
            Current response.
        *selectors:
            CSS or XPath expressions (auto-detected by leading ``//``).
        callback:
            Callback for the next page.  Defaults to ``self.parse``.
        """
        cb = callback or self.parse
        for sel in selectors:
            if sel.startswith("//"):
                href = response.xpath(sel).get()
            else:
                href = response.css(sel).get()
            if href:
                yield response.follow(href, callback=cb)
                return
