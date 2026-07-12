"""
ChromeCookieMiddleware
~~~~~~~~~~~~~~~~~~~~~~
Downloader middleware that injects cookies exported from the persistent
Chrome profile (cookies/session.json) into every Scrapy request.
"""

from __future__ import annotations

import logging

from scrapy import signals

from manual_scraper_ext.chrome_cookies import (
    apply_cookies_to_request,
    cookies_file,
    load_cookies,
    merge_cookies,
)

logger = logging.getLogger(__name__)


class ChromeCookieMiddleware:
    """Attach trusted Chrome-session cookies to outgoing requests."""

    def __init__(self, cookies=None, enabled: bool = True):
        self.enabled = enabled
        self.cookies = cookies or []

    @classmethod
    def from_crawler(cls, crawler):
        enabled = crawler.settings.getbool("CHROME_COOKIES_ENABLED", True)
        path = cookies_file(crawler.settings)
        cookies = load_cookies(path) if enabled else []
        obj = cls(cookies=cookies, enabled=enabled)
        crawler.chrome_session_cookies = list(cookies)
        crawler.signals.connect(obj.spider_opened, signal=signals.spider_opened)
        return obj

    def spider_opened(self, spider):
        if not self.enabled:
            return
        # Pick up any cookies refreshed by SeleniumChallengeMiddleware
        crawler = getattr(spider, "crawler", None)
        if crawler is not None:
            extra = getattr(crawler, "chrome_session_cookies", None)
            if extra:
                self.cookies = merge_cookies(self.cookies, extra)
        spider.logger.info(
            "[ChromeCookies] Loaded %d cookies from session file",
            len(self.cookies),
        )

    def process_request(self, request, spider):
        if not self.enabled or not self.cookies:
            return None
        crawler = getattr(spider, "crawler", None)
        if crawler is not None:
            extra = getattr(crawler, "chrome_session_cookies", None)
            if extra and len(extra) > len(self.cookies):
                self.cookies = merge_cookies(self.cookies, extra)
        apply_cookies_to_request(request, self.cookies)
        return None
