"""
qwen_captcha_middleware.py (simplified)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Scrapy downloader middleware for handling bot challenges using browser rendering.

When a challenge page is detected (4xx status with challenge keywords), this
middleware marks the request to use Playwright browser rendering instead of
simple HTTP requests. This handles most bot challenges without needing ML models.

Configuration (in custom_settings or settings.py)
--------------------------------------------------
    BROWSER_RENDERING_ENABLED : bool - enable/disable browser rendering (default True)
"""

from __future__ import annotations

import logging
from typing import Optional

import scrapy
from scrapy import signals
from scrapy.http import Request, TextResponse

logger = logging.getLogger(__name__)

# ── Challenge detection heuristics ────────────────────────────────────────────

_CHALLENGE_TITLES = (
    "just a moment",
    "attention required",
    "security check",
    "please verify",
    "are you a robot",
    "captcha",
    "human verification",
    "access denied",
    "checking your browser",
    "cloudflare",
)

_CHALLENGE_STATUS_CODES = {403, 418, 429, 503}


def _looks_like_challenge(response) -> bool:
    """Return True when the response appears to be a bot challenge page."""
    if response.status in _CHALLENGE_STATUS_CODES:
        return True
    try:
        content_type = (response.headers.get("Content-Type", b"") or b"").decode(
            "utf-8", errors="ignore"
        ).lower()
        if "text/html" not in content_type:
            return False
        title = (response.css("title::text").get() or "").lower()
        if any(kw in title for kw in _CHALLENGE_TITLES):
            return True
        body_lower = (response.text or "")[:4000].lower()
        if any(kw in body_lower for kw in _CHALLENGE_TITLES):
            return True
    except Exception:
        pass
    return False


# ── Scrapy middleware ─────────────────────────────────────────────────────────

class BrowserRenderingChallengeMiddleware:
    """
    Detects bot challenges and enables browser rendering (Playwright) to bypass them.
    
    Most e-commerce sites don't actually have CAPTCHA — they just check if the
    browser is real. Playwright handles this automatically.
    """

    def __init__(self, enabled: bool = True, crawler=None):
        self.enabled = enabled
        self._challenge_urls = set()
        self.crawler = crawler

    @classmethod
    def from_crawler(cls, crawler):
        enabled = crawler.settings.getbool("BROWSER_RENDERING_ENABLED", True)
        obj = cls(enabled=enabled, crawler=crawler)
        crawler.signals.connect(obj.spider_opened, signal=signals.spider_opened)
        return obj

    def spider_opened(self, spider):
        if self.enabled:
            spider.logger.info("[BrowserRendering] Middleware active — browser rendering for challenges enabled")

    def process_response(self, request, response, spider):
        """Inspect every response for bot challenges."""
        if not self.enabled:
            return response

        if request.url.rstrip("/").endswith("robots.txt"):
            return response

        if not _looks_like_challenge(response):
            return response

        url = request.url
        
        if url in self._challenge_urls:
            spider.logger.warning("[BrowserRendering] Challenge still present after browser rendering: %s", url)
            return response

        spider.logger.info(
            "[BrowserRendering] Challenge detected on %s (status=%d). "
            "Enabling Playwright browser rendering.",
            url, response.status,
        )

        self._challenge_urls.add(url)
        
        retry_request = request.replace(
            meta={
                **request.meta,
                "playwright": True,
                "playwright_page_methods": [
                    ("wait_for_navigation", {"timeout": 30000})
                ],
            },
            dont_filter=True,
        )
        
        return retry_request

    def process_exception(self, request, exception, spider):
        """Pass exceptions through unchanged."""
        return None


# ── Backward compatibility alias ──────────────────────────────────────────────

QwenCaptchaMiddleware = BrowserRenderingChallengeMiddleware

