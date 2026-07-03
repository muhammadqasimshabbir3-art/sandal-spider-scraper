"""
items.py
~~~~~~~~
Scrapy Item definitions for the multi-site sandal scraper framework.

``SandalItem`` is the canonical item yielded by every spider.
``SelleSandalsItem`` is preserved as a backward-compatible alias so the
existing selle_sandals.py spider continues to work without modification.
"""

import scrapy


class SandalItem(scrapy.Item):
    """Canonical item used by all spiders in the framework."""

    # Spider identity
    brand        = scrapy.Field()   # e.g. "Skechers"
    product_name = scrapy.Field()   # e.g. "GO WALK Arch Fit Sandal"
    color        = scrapy.Field()   # e.g. "Black"
    source       = scrapy.Field()   # spider name

    # Rich metadata dict written to metadata.json
    meta         = scrapy.Field()

    # Image pipeline fields
    image_urls   = scrapy.Field()   # input  – list of URLs to download
    images       = scrapy.Field()   # output – filled in by the pipeline


# ── Backward-compatible alias ──────────────────────────────────────────────────
# The original selle_sandals.py spider uses SelleSandalsItem.  Keeping it as
# an alias preserves full compatibility without touching that spider.

class SelleSandalsItem(SandalItem):
    """Backward-compatible alias for the original selle-sandals spider."""

    # Extra fields that the original spider populates via `item["meta"]`
    # are already inherited through `meta = scrapy.Field()`.

    # Keep the original field names so selle_sandals.py needs no changes.
    title  = scrapy.Field()
    url    = scrapy.Field()
