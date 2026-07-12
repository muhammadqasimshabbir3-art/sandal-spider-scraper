"""
selenium_captcha_middleware.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Downloader middleware that opens a visible Selenium Chrome browser when a
bot-challenge or CAPTCHA-like page is detected. Uses the persistent Chrome
profile (same as tools/setup_chrome_login.py) so Google login / site trust
is already present. After you press Enter, rendered HTML is returned to
Scrapy and cookies are merged back into the session jar.

Enable with setting ``SELENIUM_CHALLENGE_ENABLED = True``.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from scrapy import signals
from scrapy.http import TextResponse

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

from manual_scraper_ext.chrome_cookies import (
    cookies_file,
    load_cookies,
    merge_cookies,
    profile_dir,
    save_cookies,
    selenium_cookies_to_dict,
)
from manual_scraper_ext.qwen_captcha_middleware import _looks_like_challenge

logger = logging.getLogger(__name__)


class SeleniumChallengeMiddleware:
    """Open a visible browser (persistent profile) for CAPTCHA / challenges.

    Warning: blocks the Scrapy thread while waiting for Enter. Intended for
    interactive one-off runs.
    """

    def __init__(
        self,
        enabled: bool = False,
        crawler=None,
        profile: Optional[Path] = None,
        cookies_path: Optional[Path] = None,
    ):
        self.enabled = enabled
        self.crawler = crawler
        self.profile = profile or profile_dir()
        self.cookies_path = cookies_path or cookies_file()

    @classmethod
    def from_crawler(cls, crawler):
        enabled = crawler.settings.getbool("SELENIUM_CHALLENGE_ENABLED", False)
        obj = cls(
            enabled=enabled,
            crawler=crawler,
            profile=profile_dir(crawler.settings),
            cookies_path=cookies_file(crawler.settings),
        )
        crawler.signals.connect(obj.spider_opened, signal=signals.spider_opened)
        return obj

    def spider_opened(self, spider):
        if self.enabled:
            spider.logger.info(
                "[SeleniumChallenge] Active — profile=%s", self.profile
            )

    def _build_driver(self) -> webdriver.Chrome:
        self.profile.mkdir(parents=True, exist_ok=True)
        options = Options()
        options.add_argument("--start-maximized")
        options.add_argument(f"--user-data-dir={self.profile}")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(90)
        return driver

    def _persist_cookies(self, driver, spider) -> None:
        try:
            fresh = selenium_cookies_to_dict(driver.get_cookies())
            existing = load_cookies(self.cookies_path)
            merged = merge_cookies(existing, fresh)
            save_cookies(merged, self.cookies_path)
            if self.crawler is not None:
                self.crawler.chrome_session_cookies = merge_cookies(
                    getattr(self.crawler, "chrome_session_cookies", []) or [],
                    fresh,
                )
            spider.logger.info(
                "[SeleniumChallenge] Refreshed %d cookies → %s",
                len(merged),
                self.cookies_path,
            )
        except Exception as exc:
            spider.logger.warning(
                "[SeleniumChallenge] Cookie export failed: %s", exc
            )

    def process_response(self, request, response, spider):
        if not self.enabled:
            return response

        try:
            if not _looks_like_challenge(response):
                return response
        except Exception:
            return response

        spider.logger.info(
            "[SeleniumChallenge] Challenge on %s — launching Chrome profile",
            request.url,
        )

        driver = self._build_driver()
        try:
            driver.get(request.url)

            prompt = (
                "\n[SeleniumChallenge] Browser opened with your saved profile.\n"
                "Solve any CAPTCHA / verification, then press Enter here...\n"
            )
            print(prompt)
            try:
                input()
            except Exception:
                spider.logger.info(
                    "[SeleniumChallenge] No stdin — waiting 15s"
                )
                time.sleep(15)

            self._persist_cookies(driver, spider)

            page_source = driver.page_source or ""
            current_url = driver.current_url or request.url

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
