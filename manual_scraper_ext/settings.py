# ─────────────────────────────────────────────────────────────────────────────
#  Scrapy settings for the sandal-spider-scraper framework
#
#  This file configures the PROJECT-LEVEL defaults.  Each spider may override
#  any setting via its ``custom_settings`` class attribute.
# ─────────────────────────────────────────────────────────────────────────────

from pathlib import Path as _Path

BOT_NAME = "sandal_spider_scraper"

SPIDER_MODULES = ["manual_scraper_ext.spiders"]
NEWSPIDER_MODULE = "manual_scraper_ext.spiders"

# ── Politeness ────────────────────────────────────────────────────────────────
ROBOTSTXT_OBEY = False
CONCURRENT_REQUESTS = 2
CONCURRENT_REQUESTS_PER_DOMAIN = 2
DOWNLOAD_DELAY = 1.0            # seconds between successive requests
RANDOMIZE_DOWNLOAD_DELAY = True

# ── AutoThrottle ──────────────────────────────────────────────────────────────
AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 1.0
AUTOTHROTTLE_MAX_DELAY = 30.0
AUTOTHROTTLE_TARGET_CONCURRENCY = 1.5

# ── User-Agent ────────────────────────────────────────────────────────────────
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_REQUEST_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

# ── Retry ─────────────────────────────────────────────────────────────────────
RETRY_ENABLED = True
RETRY_TIMES = 3
RETRY_HTTP_CODES = [500, 502, 503, 504, 403, 408, 429]

# ── HTTP Cache (disabled during active scraping) ──────────────────────────────
HTTPCACHE_ENABLED = False
# Uncomment to enable caching during development:
# HTTPCACHE_ENABLED          = True
# HTTPCACHE_EXPIRATION_SECS  = 0
# HTTPCACHE_DIR              = ".scrapy/httpcache"
# HTTPCACHE_STORAGE          = "scrapy.extensions.httpcache.FilesystemCacheStorage"

# ── Item Pipelines ────────────────────────────────────────────────────────────
# The DatasetImagesPipeline is the default for all new spiders.
# The original selle-sandals spider overrides this via custom_settings.
ITEM_PIPELINES = {
    "manual_scraper_ext.pipelines.DatasetImagesPipeline": 1,
}

# ── Dataset image pipeline (new spiders) ─────────────────────────────────────
IMAGES_STORE = "dataset"           # root directory for downloaded images
IMAGES_URLS_FIELD = "image_urls"   # item field containing URL list
IMAGES_RESULT_FIELD = "images"     # item field Scrapy writes results into
IMAGES_MIN_HEIGHT = 0              # minimum image height in pixels
IMAGES_MIN_WIDTH = 0               # minimum image width in pixels

# ── Feed export (JSON Lines) ──────────────────────────────────────────────────
# Uncomment to also save scraped items to a file:
# FEEDS = {
#     "output/items.jl": {"format": "jsonlines", "overwrite": True},
# }

# ── Downloader middlewares ────────────────────────────────────────────────────
# DOWNLOADER_MIDDLEWARES = {
#     "manual_scraper_ext.middlewares.ManualScraperExtDownloaderMiddleware": 543,
# }

# ── Misc ──────────────────────────────────────────────────────────────────────
COOKIES_ENABLED = True
REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
FEED_EXPORT_ENCODING = "utf-8"

# ── Persistent Chrome profile (Google login + site trust) ─────────────────────
# Run once:  python tools/setup_chrome_login.py
# Then Scrapy reuses cookies/session.json and the same profile on challenges.
_PROJECT_ROOT = _Path(__file__).resolve().parent.parent
CHROME_PROFILE_DIR = str(_PROJECT_ROOT / "chrome_profile")
CHROME_COOKIES_FILE = str(_PROJECT_ROOT / "cookies" / "session.json")
CHROME_COOKIES_ENABLED = True

# ── Browser Rendering for Challenges ─────────────────────────────────────────
# Use Playwright browser rendering to bypass bot challenges.
# Most e-commerce sites don't have real CAPTCHA, just browser checks.
BROWSER_RENDERING_ENABLED = True

# Use Selenium to open a real browser for interactive CAPTCHA solving.
# When True, the Selenium middleware will launch a visible Chrome window
# for pages that look like bot challenges. This is intended for manual,
# one-off runs where the operator can solve the CAPTCHA interactively.
SELENIUM_CHALLENGE_ENABLED = True

# ── Skip Image Download (for testing CAPTCHA/challenges without storage) ──────
# Set to True to run spiders without saving images to dataset folder.
# Useful for testing challenge handling and validating spider logic.
SKIP_IMAGE_DOWNLOAD = False

# ── Skip Specific Spiders ─────────────────────────────────────────────────────
# List of spider names to skip during execution (e.g., ['selle-sandals']).
# Useful when running crawl_all to exclude specific sites.
# Usage: scrapy crawl all -a skip_spiders="selle-sandals,zappos"
EXCLUDED_SPIDERS = []

# Enable cookie injection + challenge middlewares
DOWNLOADER_MIDDLEWARES = {
    "manual_scraper_ext.chrome_cookie_middleware.ChromeCookieMiddleware": 540,
    # Selenium-based interactive challenge handler (runs before Playwright retry)
    "manual_scraper_ext.selenium_captcha_middleware.SeleniumChallengeMiddleware": 585,
    "manual_scraper_ext.qwen_captcha_middleware.BrowserRenderingChallengeMiddleware": 590,
}

# ── HTTP error handling ────────────────────────────────────────────────────────
# Allow 418 and 403 to reach the spider's parse methods (so they can log/skip)
# Note: individual spiders may override this list.
HTTPERROR_ALLOWED_CODES = [418, 403]

# ── Farfetch (undetected Chrome) ──────────────────────────────────────────────
# Headless is on by default in the spider; override on CLI if needed:
#   scrapy crawl farfetch -s FARFETCH_SELENIUM_HEADLESS=False
FARFETCH_SELENIUM_HEADLESS = True
FARFETCH_PDP_WAIT = 6
FARFETCH_CDN_WORKERS = 8
FARFETCH_RESUME_MIN_IMAGES = 4

# ── Nordstrom (undetected Chrome) ─────────────────────────────────────────────
# True --headless is blocked (invitation.html). Default uses offscreen Chrome
# (fast, no visible window, catalog works):
#   scrapy crawl nordstrom
# True headless (usually blocked): -s NORDSTROM_HEADLESS_STYLE=real
# Visible window: -s NORDSTROM_SELENIUM_HEADLESS=False
NORDSTROM_SELENIUM_HEADLESS = True
NORDSTROM_HEADLESS_STYLE = "offscreen"
NORDSTROM_PDP_WAIT = 5
NORDSTROM_RESUME_MIN_IMAGES = 4

# ── Logging Configuration ─────────────────────────────────────────────────────
# For Kaggle/limited output environments:
#   LOG_LEVEL = 'WARNING'  — Show only warnings and errors (quiet mode)
#   LOG_LEVEL = 'INFO'     — Show info messages (default)
#   LOG_LEVEL = 'DEBUG'    — Show all debug messages (verbose)
LOG_LEVEL = 'INFO'

# Disable specific verbose loggers
LOGSTATS_INTERVAL = 0  # Disable periodic log stats (0 = never print stats)

# Suppress Python warnings in quiet mode
# Uncomment for Kaggle to hide deprecation warnings:
# import warnings
# warnings.filterwarnings('ignore')
