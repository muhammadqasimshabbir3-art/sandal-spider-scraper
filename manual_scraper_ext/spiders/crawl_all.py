"""
crawl_all.py
~~~~~~~~~~~~
Meta-spider that runs every active sandal spider.

Usage::

    scrapy crawl all

Active sites:
  nordstrom, farfetch, zappos, selle-sandals
"""

from __future__ import annotations

import scrapy

from manual_scraper_ext.base.base_spider import _BASE_CUSTOM_SETTINGS


def _all_spider_classes():
    """Return list of active spider classes (excluding this meta spider)."""
    from manual_scraper_ext.spiders.nordstrom import NordstromSpider
    from manual_scraper_ext.spiders.farfetch import FarfetchSpider
    from manual_scraper_ext.spiders.zappos import ZapposSpider
    from manual_scraper_ext.spiders.selle_sandals import SelleSandalsSpider

    return [
        NordstromSpider,
        FarfetchSpider,
        ZapposSpider,
        SelleSandalsSpider,
    ]


class AllSandalsSpider(scrapy.Spider):
    """Dispatch start requests from every active site spider."""

    name = "all"

    custom_settings = dict(_BASE_CUSTOM_SETTINGS)

    async def start(self):
        spider_classes = _all_spider_classes()
        excluded = set(self.settings.getlist("EXCLUDED_SPIDERS") or [])
        skip_arg = getattr(self, "skip_spiders", "") or ""
        if skip_arg:
            excluded.update(s.strip() for s in skip_arg.split(",") if s.strip())

        for spider_cls in spider_classes:
            if spider_cls.name in excluded:
                self.logger.info("Skipping excluded spider: %s", spider_cls.name)
                continue
            self.logger.info("Queueing start URLs for spider: %s", spider_cls.name)
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
                    "Error running start for %s: %s", spider_cls.name, e
                )

    def parse(self, response):
        self.logger.warning(
            "AllSandalsSpider.parse called for %s — routing fallback.",
            response.url,
        )
        return []
