"""
selenium_captcha_middleware.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Downloader middleware that opens a visible Selenium Chrome browser when a
bot-challenge or CAPTCHA-like page is detected. The user can manually solve
the challenge in the opened browser; pressing Enter in the terminal will
capture the rendered HTML and return it to Scrapy as a `TextResponse`.

Enable with setting `SELENIUM_CHALLENGE_ENABLED = True` (defaults to False).
"""

from __future__ import annotations

import logging
from typing import Optional

from scrapy import signals
from scrapy.http import TextResponse

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

from manual_scraper_ext.qwen_captcha_middleware import _looks_like_challenge

logger = logging.getLogger(__name__)


class SeleniumChallengeMiddleware:
    """Open a visible browser to let the user solve CAPTCHAs or challenges.

    Warning: this middleware blocks the Scrapy thread while waiting for the
    user to solve the challenge and press Enter. Use for one-off/manual runs
    where interactive solving is required.
    """

    def __init__(self, enabled: bool = False, crawler=None):
        self.enabled = enabled
        self.crawler = crawler

    @classmethod
    def from_crawler(cls, crawler):
        enabled = crawler.settings.getbool("SELENIUM_CHALLENGE_ENABLED", False)
        obj = cls(enabled=enabled, crawler=crawler)
        crawler.signals.connect(obj.spider_opened, signal=signals.spider_opened)
        return obj

    def spider_opened(self, spider):
        if self.enabled:
            spider.logger.info("[SeleniumChallenge] Middleware active — Selenium enabled for challenges")

    def process_response(self, request, response, spider):
        if not self.enabled:
            return response

        # Use the same heuristic as the existing challenge middleware
        try:
            if not _looks_like_challenge(response):
                return response
        except Exception:
            return response

        spider.logger.info("[SeleniumChallenge] Challenge detected on %s — launching Selenium", request.url)

        # Launch Chrome (visible) and navigate to the URL
        options = webdriver.ChromeOptions()
        options.add_argument("--start-maximized")
        # Do not run headless — user must solve CAPTCHA interactively

        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)

        try:
            driver.get(request.url)

            # Inform the user to solve the challenge interactively
            prompt = (
                "\n[SeleniumChallenge] A browser window has been opened. "
                "Please solve the CAPTCHA or verification in the browser, then press Enter here to continue...\n"
            )
            print(prompt)
            try:
                input()  # wait for user to confirm they solved the challenge
            except Exception:
                # If input is not available (non-interactive), fall back to waiting a bit
                spider.logger.info("[SeleniumChallenge] No interactive stdin available — waiting 10s for manual solve")
                import time

                time.sleep(10)

            page_source = driver.page_source or ""
            current_url = driver.current_url or request.url

            # Build a TextResponse for Scrapy using the rendered HTML
            return TextResponse(
                url=current_url,
                body=page_source,
                encoding="utf-8",
                request=request,
            )

        finally:
            try:
                driver.quit()
            except Exception:
                pass

