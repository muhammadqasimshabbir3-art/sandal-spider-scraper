"""
Middlewares for the selle-sandals scraper.

Currently only a skeleton is provided.  Add custom middleware classes here
as needed (e.g. rotating user-agents, proxy rotation, retry logic, etc.).
"""

from scrapy import signals


class ManualScraperExtSpiderMiddleware:
    """Default spider middleware – pass-through."""

    @classmethod
    def from_crawler(cls, crawler):
        s = cls()
        crawler.signals.connect(s.spider_opened, signal=signals.spider_opened)
        return s

    def process_spider_output(self, response, result, spider):
        for i in result:
            yield i

    def process_spider_exception(self, response, exception, spider):
        pass

    def spider_opened(self, spider):
        spider.logger.debug("Spider opened: %s" % spider.name)


class ManualScraperExtDownloaderMiddleware:
    """Default downloader middleware – pass-through."""

    @classmethod
    def from_crawler(cls, crawler):
        s = cls()
        crawler.signals.connect(s.spider_opened, signal=signals.spider_opened)
        return s

    def process_request(self, request, spider):
        return None

    def process_response(self, request, response, spider):
        return response

    def process_exception(self, request, exception, spider):
        pass

    def spider_opened(self, spider):
        spider.logger.debug("Spider opened: %s" % spider.name)
