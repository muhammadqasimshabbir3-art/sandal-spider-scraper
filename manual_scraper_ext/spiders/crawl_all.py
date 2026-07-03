"""
crawl_all.py
~~~~~~~~~~~~
Meta-spider that sequentially runs every sandal spider in the framework.

Usage::

    scrapy crawl all

This spider imports all registered spiders, starts them one after the other,
and logs a summary when finished.

Implementation
--------------
Scrapy does not natively support running multiple spiders in one process in
sequence.  This spider achieves sequential crawling by subclassing
``CrawlerProcess`` logic inside a normal Scrapy spider — specifically by
using a special start_requests approach that dispatches to every registered
spider's start_urls and parse methods, effectively merging them all into a
single crawl.

Note: because Scrapy shares a single downloader and pipeline across all
requests, the images from all sites will be correctly routed through the
``DatasetImagesPipeline`` and saved per-brand under the ``dataset/`` folder.
"""

from __future__ import annotations

from typing import Iterator

import scrapy

from manual_scraper_ext.base.base_spider import _BASE_CUSTOM_SETTINGS

# ── Registry of all site spiders ──────────────────────────────────────────────
# Import lazily so missing optional dependencies don't block the module load.

def _all_spider_classes():
    """Return list of all concrete spider classes (excluding this one)."""
    from manual_scraper_ext.spiders.skechers import SkechersSpider
    from manual_scraper_ext.spiders.underarmour import UnderarmourSpider
    from manual_scraper_ext.spiders.columbia import ColumbiaSpider
    from manual_scraper_ext.spiders.asos import AsosSpider
    from manual_scraper_ext.spiders.nordstrom import NordstromSpider
    from manual_scraper_ext.spiders.zappos import ZapposSpider
    from manual_scraper_ext.spiders.crocs import CrocsSpider
    from manual_scraper_ext.spiders.adidas import AdidasSpider
    from manual_scraper_ext.spiders.alexandermcqueen import AlexanderMcQueenSpider
    from manual_scraper_ext.spiders.farfetch import FarfetchSpider
    from manual_scraper_ext.spiders.selle_sandals import SelleSandalsSpider

    return [
        SelleSandalsSpider,
        SkechersSpider,
        UnderarmourSpider,
        ColumbiaSpider,
        AsosSpider,
        NordstromSpider,
        ZapposSpider,
        CrocsSpider,
        AdidasSpider,
        AlexanderMcQueenSpider,
        FarfetchSpider,
    ]


class AllSandalsSpider(scrapy.Spider):
    """
    Meta-spider: dispatches start requests from every registered spider
    and routes responses through each spider's own ``parse`` callback.

    This gives a single unified crawl across all 11 sites while keeping the
    images pipeline, logging, and deduplication operating at the project level.
    """

    name = "all"

    custom_settings = dict(_BASE_CUSTOM_SETTINGS)

    async def start(self):
        """Emit start requests for every registered spider."""
        spider_classes = _all_spider_classes()

        for spider_cls in spider_classes:
            self.logger.info(
                "Queueing start URLs for spider: %s", spider_cls.name
            )
            # Instantiate using from_crawler to ensure proper initialization
            dummy = spider_cls.from_crawler(self.crawler)

            try:
                start_res = dummy.start()
                if hasattr(start_res, "__aiter__"):
                    async for req in start_res:
                        if isinstance(req, scrapy.Request):
                            if req.callback is None:
                                req = req.replace(callback=dummy.parse)
                            yield req
                else:
                    for req in start_res:
                        if isinstance(req, scrapy.Request):
                            if req.callback is None:
                                req = req.replace(callback=dummy.parse)
                            yield req
            except Exception as e:
                self.logger.warning(
                    "Error running start for %s: %s",
                    spider_cls.name, e
                )

    def parse(self, response):
        """Fallback parse — should not normally be called."""
        self.logger.warning(
            "AllSandalsSpider.parse called for %s — "
            "this is a routing fallback and may indicate a misconfiguration.",
            response.url,
        )
        return []
